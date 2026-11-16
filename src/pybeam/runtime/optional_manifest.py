import hashlib
import platform
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

MAX_MANIFEST_BYTES = 262_144
MAX_FILES = 1024


def relative_path(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts or len(str(path)) > 512:
        raise ValueError("Optional paths must be bounded and relative")
    return path


type AssetPath = Annotated[Path, AfterValidator(relative_path)]
type Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
type Version = Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)]
type SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Revision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ModuleIdentity(FrozenModel):
    name: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.@]*$", max_length=255)]
    md5: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class ApplicationAsset(FrozenModel):
    name: Name
    version: Version
    code_path: AssetPath
    dependencies: Annotated[tuple[Name, ...], Field(max_length=16)]
    modules: Annotated[tuple[ModuleIdentity, ...], Field(min_length=1, max_length=512)]


class AssetFile(FrozenModel):
    path: AssetPath
    sha256: SHA256


class Sources(FrozenModel):
    elixir: Revision
    gen_stage: Revision


class OptionalManifest(FrozenModel):
    format_version: Annotated[int, Field(ge=1, le=1)]
    name: Name
    otp_version: Version
    erts_version: Version
    sources: Sources
    applications: Annotated[tuple[ApplicationAsset, ...], Field(min_length=1, max_length=8)]
    files: Annotated[tuple[AssetFile, ...], Field(min_length=1, max_length=MAX_FILES)]


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
    return OptionalManifest.model_validate_json(path.read_bytes())


def approved_manifest(
    name: str, *, system: str | None = None, architecture: str | None = None
) -> OptionalManifest:
    directory = Path(__file__).with_name("optional_specs")
    system = platform.system() if system is None else system
    architecture = platform.machine() if architecture is None else architecture
    if (system, architecture) == ("Linux", "x86_64"):
        suffix = "-linux_x86_64"
    elif (system, architecture) == ("Darwin", "arm64"):
        suffix = ""
    else:
        raise ValueError("Unsupported optional runtime platform")
    file = contained(directory, Path(name + suffix + ".json"))
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
    (root / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
