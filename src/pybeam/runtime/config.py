import math
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pybeam.codec import CodecError, Json


class BootstrapError(RuntimeError):
    exit_status: int = 1


class CleanupError(BootstrapError):
    def __init__(self, message: str, entry_status: int) -> None:
        super().__init__(message)
        self.entry_status = entry_status
        self.exit_status = entry_status or 1


class Mode(StrEnum):
    MANAGED = "managed"
    EXTERNAL = "external"
    CUSTOM = "custom"


def _port(value: int, name: str) -> None:
    if type(value) is not int or not 20000 <= value <= 60000:
        raise ValueError(name + " must be an integer from 20000 to 60000")


def _timeout(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < math.inf:
        raise ValueError(name + " must be a positive finite number")


@dataclass(frozen=True, slots=True, kw_only=True)
class PythonNode:
    alias: Literal["a", "b"]
    entry: Path
    port: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry", self.entry.resolve())
        _port(self.port, "port")


@dataclass(frozen=True, slots=True, kw_only=True)
class Distribution:
    """Two trusted local nodes. No remote hosts, implicit retries, or callable transfer."""

    nodes: tuple[PythonNode, PythonNode]
    discovery_port: int
    capacity: int
    max_bytes: int

    def __post_init__(self) -> None:
        _port(self.discovery_port, "discovery_port")
        if type(self.capacity) is not int or not 1 <= self.capacity <= 8:
            raise ValueError("capacity must be an integer from 1 to 8")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 16384:
            raise ValueError("max_bytes must be an integer from 1 to 16384")
        if {node.alias for node in self.nodes} != {"a", "b"}:
            raise ValueError("Declare exactly aliases a and b")
        if len({self.discovery_port, *(node.port for node in self.nodes)}) != 3:
            raise ValueError("Discovery and node ports must be distinct")
        if not all(node.entry.is_file() for node in self.nodes):
            raise ValueError("Every node must have an existing Python entry file")


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    development_runtime: Path | None = None
    startup_timeout: float = 30.0
    shutdown_timeout: float = 5.0
    mode: Mode = Mode.MANAGED
    workers: Literal[1] = 1
    distribution: Literal[False] | Distribution = False
    listeners: bool = False

    def __post_init__(self) -> None:
        if self.development_runtime is not None:
            object.__setattr__(self, "development_runtime", self.development_runtime.resolve())
        _timeout(self.startup_timeout, "startup_timeout")
        _timeout(self.shutdown_timeout, "shutdown_timeout")
        if self.workers != 1:
            raise ValueError("workers must be 1")
        if self.distribution is not False and not isinstance(self.distribution, Distribution):
            raise ValueError("distribution must be false or an explicit two-node configuration")
        if self.mode is not Mode.MANAGED:
            raise ValueError(
                "Runtime mode '{}' is not implemented; only owned managed runtimes are supported".format(
                    self.mode
                )
            )
        if self.listeners:
            raise ValueError("listeners must be false; managed bootstrap opens no listeners")


CONFIG = Json(Config)


def _path(value: object, directory: Path) -> object:
    if not isinstance(value, str):
        return value
    if not value:
        raise BootstrapError("Invalid tool.pybeam configuration: paths must not be empty")
    return str(directory / value)


def _anchor(value: object, directory: Path) -> object:
    """Resolve relative paths against the configuration file's directory."""
    if not isinstance(value, dict):
        return value
    document = dict(value)
    distribution = document.get("distribution", False)
    if distribution is not False and not isinstance(distribution, dict):
        raise BootstrapError(
            "Invalid tool.pybeam configuration: distribution must be false or an explicit "
            "two-node configuration"
        )
    if "development_runtime" in document:
        document["development_runtime"] = _path(document["development_runtime"], directory)
    distribution = document.get("distribution")
    if isinstance(distribution, dict) and isinstance(distribution.get("nodes"), list):
        distribution = dict(distribution)
        distribution["nodes"] = [
            {**node, "entry": _path(node.get("entry"), directory)}
            if isinstance(node, dict) and "entry" in node
            else node
            for node in distribution["nodes"]
        ]
        document["distribution"] = distribution
    return document


def parse_config(value: object, directory: Path) -> Config:
    try:
        return CONFIG.load(_anchor(value, directory))
    except CodecError as error:
        raise BootstrapError("Invalid tool.pybeam configuration: " + str(error)) from error


def load_config(directory: Path) -> Config:
    for parent in (directory, *directory.parents):
        project = parent / "pyproject.toml"
        if project.is_file():
            try:
                with project.open("rb") as source:
                    document = tomllib.load(source)
            except (OSError, tomllib.TOMLDecodeError) as error:
                raise BootstrapError("Cannot read configuration: " + str(project)) from error
            tool = document.get("tool", {})
            if not isinstance(tool, dict):
                raise BootstrapError("Invalid tool.pybeam configuration: tool must be a table")
            return parse_config(tool.get("pybeam", {}), parent)
    return Config()
