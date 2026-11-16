import asyncio
import platform
from pathlib import Path

from pybeam.runtime.assets import _contained
from pybeam.runtime.config import BootstrapError
from pybeam.runtime.loader import require_managed_entry
from pybeam.runtime.optional_manifest import OptionalManifest, verify_manifest


def read_manifest(root: Path) -> OptionalManifest:
    if platform.system() == "Linux" and root.name == "genstage":
        raise BootstrapError("The optional GenStage payload is not bundled on Linux")
    try:
        return verify_manifest(root)
    except (OSError, ValueError) as error:
        raise BootstrapError(
            "Optional runtime artifact is unavailable or invalid: " + str(root)
        ) from error


async def start_optional(name: str) -> None:
    """Verify approved wheel-relative assets and start their declared OTP applications.

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
