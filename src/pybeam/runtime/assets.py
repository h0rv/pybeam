import platform
import re
import shutil
import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from pybeam.runtime.config import BootstrapError, Config


@dataclass(frozen=True, slots=True)
class RuntimeAssets:
    executable: Path
    code_paths: tuple[Path, ...]
    root_dir: Path | None = None
    bindir: Path | None = None
    boot: Path | None = None
    python_home: Path | None = None
    python_library: Path | None = None

    @property
    def environment(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.root_dir is not None and self.bindir is not None:
            result.update(
                ROOTDIR=str(self.root_dir), BINDIR=str(self.bindir), EMU="beam", PROGNAME="erl"
            )
        if self.python_home is not None:
            result["PYTHONHOME"] = str(self.python_home)
        if self.python_library is not None:
            result["DYLD_LIBRARY_PATH"] = str(self.python_library.parent)
        return result


def _relative(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Packaged runtime paths must stay inside their asset directory")
    return path


type AssetPath = Annotated[Path, AfterValidator(_relative)]


class RuntimeManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    python_version: Literal["3.14"]
    implementation: Literal["cpython"]
    architecture: Literal["arm64"]
    system: Literal["Darwin"]
    minimum_macos: tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=0)]]
    wheel_tag: Annotated[str, Field(pattern=r"^cp314-cp314-macosx_\d+_\d+_arm64$")]
    executable: AssetPath
    root_dir: Literal["."]
    bindir: AssetPath
    code_paths: Annotated[tuple[AssetPath, ...], Field(min_length=1)]
    erts_version: Annotated[str, Field(pattern=r"^[0-9.]+$")]
    python_library: Literal["libpython3.14.dylib"]


def _version() -> str:
    suffix = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
    return "{}.{}{}".format(sys.version_info.major, sys.version_info.minor, suffix)


def _contained(root: Path, value: Path) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise BootstrapError("Packaged runtime assets must stay inside their asset directory")
    return path


def _host_python_library(name: str) -> Path:
    home = Path(sys.base_prefix).resolve()
    candidates = [home / "lib" / name]
    libdir: object = sysconfig.get_config_var("LIBDIR")
    if isinstance(libdir, str) and Path(libdir).resolve().is_relative_to(home):
        candidates.append(Path(libdir) / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise BootstrapError("Host CPython must provide a matching shared library: " + name)


def packaged_assets(root: Path) -> RuntimeAssets:
    try:
        manifest = RuntimeManifest.model_validate_json((root / "runtime.json").read_text())
    except OSError as error:
        raise BootstrapError("Cannot read packaged runtime manifest") from error
    except ValidationError as error:
        raise BootstrapError(
            "Packaged runtime is not compatible with Python "
            + _version()
            + " or has an invalid manifest: "
            + str(error)
        ) from error
    if (
        _version() != manifest.python_version
        or sys.implementation.name != manifest.implementation
        or platform.system() != manifest.system
        or platform.machine() != manifest.architecture
    ):
        raise BootstrapError(
            "Packaged runtime is not compatible with this host Python/OS/architecture"
        )
    version = platform.mac_ver()[0].split(".")
    if len(version) < 2 or (int(version[0]), int(version[1])) < manifest.minimum_macos:
        raise BootstrapError(
            "Packaged runtime requires macOS {}.{} or newer".format(*manifest.minimum_macos)
        )
    root = root.resolve()
    executable = _contained(root, manifest.executable)
    bindir = _contained(root, manifest.bindir)
    paths = tuple(_contained(root, item) for item in manifest.code_paths)
    if (
        executable != bindir / "erlexec"
        or bindir != root / ("erts-" + manifest.erts_version) / "bin"
    ):
        raise BootstrapError("Packaged runtime must use native OTP erlexec")
    _validate_files(executable, paths)
    required = (
        root / "bin" / "start.boot",
        *(bindir / name for name in ("beam.smp", "erl_child_setup", "inet_gethost")),
        *(
            path.parent / "priv" / "py_nif.so"
            for path in paths
            if (path / "erlang_python.app").is_file()
        ),
    )
    if not all(path.is_file() for path in required):
        raise BootstrapError(
            "Packaged runtime assets are incomplete (boot, ERTS, or native library missing)"
        )
    return RuntimeAssets(
        executable,
        paths,
        root,
        bindir,
        root / "bin" / "start",
        Path(sys.base_prefix).resolve(),
        _host_python_library(manifest.python_library),
    )


def _validate_files(executable: Path, paths: tuple[Path, ...]) -> None:
    if not executable.is_file() or not paths or not all(path.is_dir() for path in paths):
        raise BootstrapError("Runtime assets are incomplete")
    if not any((path / "py_launch.beam").is_file() for path in paths):
        raise BootstrapError("Runtime is missing py_launch.beam; rebuild the native runtime")
    if not any((path / "erlang_python.app").is_file() for path in paths):
        raise BootstrapError("Runtime is missing the erlang_python application")


def development_assets(root: Path) -> RuntimeAssets:
    executable = shutil.which("erl")
    if executable is None:
        raise BootstrapError("Development runtime needs an installed ERTS executable (erl)")
    paths = tuple(sorted((*root.glob("lib/*/ebin"), *root.glob("checkouts/*/ebin"))))
    _validate_files(Path(executable), paths)
    native = tuple(
        candidate
        for path in paths
        if (path / "erlang_python.app").is_file()
        for candidate in (path.parent / "priv").glob("py_nif.*")
        if candidate.suffix in {".so", ".dylib"}
    )
    if len(native) != 1:
        raise BootstrapError("Development runtime needs exactly one built py_nif shared library")
    data = native[0].read_bytes()
    versions = set(re.findall(rb"libpython(3\.[0-9]+t?)[./]", data))
    versions.update(re.findall(rb"Python\.framework/Versions/(3\.[0-9]+t?)/", data))
    if versions != {_version().encode()}:
        raise BootstrapError(
            "Cannot verify development NIF compatibility with Python "
            + _version()
            + "; rebuild with this Python's python-config (do not load a mismatched NIF)"
        )
    return RuntimeAssets(Path(executable), paths)


def discover_assets(config: Config) -> RuntimeAssets:
    if config.development_runtime is not None:
        return development_assets(config.development_runtime)
    packaged = Path(__file__).with_name("_assets")
    if (packaged / "runtime.json").is_file():
        return packaged_assets(packaged)
    raise BootstrapError(
        "No supported managed runtime is installed for Python "
        + _version()
        + ". Install a python-beam wheel containing compatible runtime assets, or set "
        + "[tool.pybeam].development_runtime to an already-built compatible development runtime. "
        + "Installing the Python source package alone does not install ERTS or the native extension."
    )
