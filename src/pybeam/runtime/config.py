import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)


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


type Timeout = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
type RuntimePath = Annotated[str, Field(strict=True, min_length=1)]
_PATH_TEXT = TypeAdapter(RuntimePath)


type PrivatePort = Annotated[int, Field(strict=True, ge=20000, le=60000)]


class PythonNode(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    alias: Literal["a", "b"]
    entry: Path
    port: PrivatePort

    @field_validator("entry", mode="plain")
    @classmethod
    def resolve_entry(cls, value: object, info: ValidationInfo) -> Path:
        if isinstance(value, Path):
            return value.resolve()
        text = _PATH_TEXT.validate_python(value)
        directory: object = info.context
        return ((directory if isinstance(directory, Path) else Path.cwd()) / text).resolve()


class Distribution(BaseModel):
    """Two trusted local nodes. No remote hosts, implicit retries, or callable transfer."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    nodes: tuple[PythonNode, PythonNode]
    discovery_port: PrivatePort
    capacity: Annotated[int, Field(strict=True, ge=1, le=8)]
    max_bytes: Annotated[int, Field(strict=True, ge=1, le=16384)]

    @field_validator("nodes", mode="before")
    @classmethod
    def parse_nodes(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def distinct_nodes(self) -> Self:
        if {node.alias for node in self.nodes} != {"a", "b"}:
            raise ValueError("Declare exactly aliases a and b")
        if len({self.discovery_port, *(node.port for node in self.nodes)}) != 3:
            raise ValueError("Discovery and node ports must be distinct")
        if not all(node.entry.is_file() for node in self.nodes):
            raise ValueError("Every node must have an existing Python entry file")
        return self


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", validate_default=True)

    development_runtime: Path | None = None
    startup_timeout: Timeout = 30.0
    shutdown_timeout: Timeout = 5.0
    mode: Mode = Mode.MANAGED
    workers: Annotated[int, Field(ge=1, le=1)] = 1
    distribution: Literal[False] | Distribution = False
    listeners: bool = False

    @field_validator("distribution", mode="before")
    @classmethod
    def parse_distribution(cls, value: object) -> object:
        if value is False or isinstance(value, Distribution | dict):
            return value
        raise ValueError("distribution must be false or an explicit two-node configuration")

    @field_validator("mode", mode="before")
    @classmethod
    def parse_mode(cls, value: object) -> Mode:
        return Mode(TypeAdapter(str, config=ConfigDict(strict=True)).validate_python(value))

    @field_validator("development_runtime", mode="plain")
    @classmethod
    def resolve_runtime(cls, value: object, info: ValidationInfo) -> Path | None:
        if value is None or isinstance(value, Path):
            return value
        text = _PATH_TEXT.validate_python(value)
        directory: object = info.context
        if isinstance(directory, Path):
            return (directory / text).resolve()
        return Path(text).resolve()

    @model_validator(mode="after")
    def supported_capabilities(self) -> Self:
        if self.mode is not Mode.MANAGED:
            raise ValueError(
                "Runtime mode '{}' is not implemented; only owned managed runtimes are supported".format(
                    self.mode
                )
            )
        if self.listeners:
            raise ValueError("listeners must be false; managed bootstrap opens no listeners")
        return self


class _Tool(BaseModel):
    model_config = ConfigDict(strict=True)

    pybeam: Config = Field(default_factory=Config)


class _Project(BaseModel):
    model_config = ConfigDict(strict=True)

    tool: _Tool = Field(default_factory=_Tool)


def parse_config(value: object, directory: Path) -> Config:
    try:
        return Config.model_validate(value, context=directory)
    except ValidationError as error:
        raise BootstrapError("Invalid tool.pybeam configuration: " + str(error)) from error


def load_config(directory: Path) -> Config:
    for parent in (directory, *directory.parents):
        project = parent / "pyproject.toml"
        if project.is_file():
            try:
                with project.open("rb") as source:
                    document = tomllib.load(source)
                return _Project.model_validate(document, context=parent).tool.pybeam
            except ValidationError as error:
                raise BootstrapError("Invalid tool.pybeam configuration: " + str(error)) from error
            except (OSError, tomllib.TOMLDecodeError) as error:
                raise BootstrapError("Cannot read configuration: " + str(project)) from error
    return Config()
