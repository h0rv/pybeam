import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pybeam.codec import Json

MAX_MANIFEST_BYTES = 262_144
MAX_FILES = 1024

_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_.@]{0,254}")
_MD5 = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


def relative_path(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts or len(str(path)) > 512:
        raise ValueError("Optional paths must be bounded and relative")
    return path


def _match(pattern: re.Pattern[str], value: str, name: str) -> None:
    if pattern.fullmatch(value) is None:
        raise ValueError(name + " has an invalid format")


def _version(value: str, name: str) -> None:
    if len(value) > 32:
        raise ValueError(name + " is too long")
    _match(_VERSION, value, name)


def _count(items: tuple[object, ...], low: int, high: int, name: str) -> None:
    if not low <= len(items) <= high:
        raise ValueError(name + " must have " + str(low) + " to " + str(high) + " items")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModuleIdentity:
    name: str
    md5: str

    def __post_init__(self) -> None:
        _match(_MODULE, self.name, "module name")
        _match(_MD5, self.md5, "md5")


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationAsset:
    name: str
    version: str
    code_path: Path
    dependencies: tuple[str, ...]
    modules: tuple[ModuleIdentity, ...]

    def __post_init__(self) -> None:
        _match(_NAME, self.name, "name")
        _version(self.version, "version")
        relative_path(self.code_path)
        _count(self.dependencies, 0, 16, "dependencies")
        for dependency in self.dependencies:
            _match(_NAME, dependency, "dependency")
        _count(self.modules, 1, 512, "modules")


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetFile:
    path: Path
    sha256: str

    def __post_init__(self) -> None:
        relative_path(self.path)
        _match(_SHA256, self.sha256, "sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class Sources:
    elixir: str
    gen_stage: str

    def __post_init__(self) -> None:
        _match(_REVISION, self.elixir, "elixir revision")
        _match(_REVISION, self.gen_stage, "gen_stage revision")


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionalManifest:
    format_version: Literal[1]
    name: str
    otp_version: str
    erts_version: str
    sources: Sources
    applications: tuple[ApplicationAsset, ...]
    files: tuple[AssetFile, ...]

    def __post_init__(self) -> None:
        _match(_NAME, self.name, "name")
        _version(self.otp_version, "otp_version")
        _version(self.erts_version, "erts_version")
        _count(self.applications, 1, 8, "applications")
        _count(self.files, 1, MAX_FILES, "files")


MANIFEST = Json(OptionalManifest)


def contained(root: Path, relative: Path) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Optional asset escaped its root")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("Optional assets cannot contain symlinks")
    return path


def parse_manifest(path: Path) -> OptionalManifest:
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Optional manifest exceeds its supported size")
    return MANIFEST.decode(path.read_bytes())


def approved_manifest(name: str) -> OptionalManifest:
    directory = Path(__file__).with_name("optional_specs")
    file = contained(directory, Path(name + ".json"))
    manifest = parse_manifest(file)
    if manifest.name != name:
        raise ValueError("Optional specification name does not match its directory")
    return manifest


def verify_manifest(root: Path) -> OptionalManifest:
    manifest = parse_manifest(contained(root, Path("manifest.json")))
    approved = approved_manifest(manifest.name)
    if manifest != approved:
        raise ValueError("Optional manifest does not match the approved pinned specification")
    expected = {item.path for item in approved.files}
    if len(expected) != len(approved.files):
        raise ValueError("Optional specification contains duplicate file paths")
    actual: set[Path] = set()
    for index, path in enumerate(root.rglob("*")):
        if index >= MAX_FILES * 4 or path.is_symlink():
            raise ValueError("Optional asset tree exceeds its supported shape")
        if path.is_file():
            actual.add(path.relative_to(root))
    if actual != expected | {Path("manifest.json")}:
        raise ValueError("Optional runtime contains undeclared or missing files")
    for item in approved.files:
        path = contained(root, item.path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256:
            raise ValueError("Optional runtime asset hash mismatch: " + str(item.path))
    return manifest


def write_manifest(root: Path, manifest: OptionalManifest) -> None:
    (root / "manifest.json").write_text(json.dumps(MANIFEST.dump(manifest), indent=2) + "\n")
