import asyncio
import hashlib
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pybeam.runtime.assets import AssetPath, _contained
from pybeam.runtime.config import BootstrapError
from pybeam.runtime.loader import require_managed_entry

type Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
type Version = Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$")]


class ApplicationAsset(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: Name
    version: Version
    code_path: AssetPath


class AssetFile(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    path: AssetPath
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class OptionalManifest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: Name
    otp_version: Version
    erts_version: Version
    sources: dict[Name, Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]]
    applications: Annotated[tuple[ApplicationAsset, ...], Field(min_length=1, max_length=16)]
    files: Annotated[tuple[AssetFile, ...], Field(min_length=1)]


def read_manifest(root: Path) -> OptionalManifest:
    try:
        manifest = OptionalManifest.model_validate_json((root / "manifest.json").read_bytes())
        seen: set[Path] = set()
        for asset in manifest.files:
            path = _contained(root, asset.path)
            if path in seen or (root / asset.path).is_symlink():
                raise ValueError("Optional assets must be unique regular files")
            seen.add(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
                raise ValueError("Optional runtime asset hash mismatch: " + str(asset.path))
        for application in manifest.applications:
            directory = _contained(root, application.code_path)
            app = directory / (application.name + ".app")
            if app not in seen or not directory.is_dir():
                raise ValueError("Optional application is missing from its manifest")
            for path in directory.iterdir():
                if path.suffix not in {".beam", ".app"} or path.resolve() not in seen:
                    raise ValueError("Optional code path contains an undeclared file")
        return manifest
    except (OSError, ValueError, ValidationError) as error:
        raise BootstrapError(
            "Optional runtime artifact is unavailable or invalid: " + str(root)
        ) from error


async def start_optional(name: str) -> None:
    """Verify wheel-relative assets and start only their declared OTP applications.

    No tools, network access, VM launch or development environment lookup occurs.
    Applications share the managed VM and remain loaded until it shuts down.
    """
    require_managed_entry()
    root = _contained(Path(__file__).with_name("_assets") / "optional", Path(name))
    manifest = await asyncio.to_thread(read_manifest, root)
    if manifest.name != name:
        raise BootstrapError("Optional runtime artifact name does not match its directory")
    try:
        import erlang
    except ImportError as error:
        raise BootstrapError("Optional applications require a managed BEAM runtime") from error

    try:
        result = await erlang.async_call(
            "_application_start",
            [str(_contained(root, app.code_path)).encode() for app in manifest.applications],
            [(app.name.encode(), app.version.encode()) for app in manifest.applications],
            manifest.erts_version.encode(),
        )
    except RuntimeError as error:
        raise BootstrapError("Optional OTP application startup failed for " + name) from error
    if result != "ok":
        raise BootstrapError("Optional OTP application startup failed: " + str(result))
