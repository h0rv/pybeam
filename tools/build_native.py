import fcntl
import os
import platform
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# GNU make propagates flags/assignments, injected makefiles and recursive state.
# Strip only these controls, retaining compiler and toolchain configuration.
MAKE_CONTROLS = frozenset(
    {
        "MAKEFLAGS",
        "MFLAGS",
        "GNUMAKEFLAGS",
        "MAKEOVERRIDES",
        "MAKEFILES",
        "MAKE",
        "MAKELEVEL",
    }
)


def compiler_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if MAKE_CONTROLS.intersection(environment):
        print("Inherited make controls replaced by the compiler job budget", file=sys.stderr)
    return {name: value for name, value in environment.items() if name not in MAKE_CONTROLS}


def main() -> None:
    if sys.version_info[:3] != (3, 14, 10) or sysconfig.get_config_var("Py_GIL_DISABLED") == 1:
        raise RuntimeError("Native development builds require conventional CPython 3.14.10")
    config = Path(sys.base_prefix) / "bin/python3.14-config"
    if not config.is_file():
        raise RuntimeError("The selected mise Python is missing python3.14-config")
    original_getconf, rebar = shutil.which("getconf"), shutil.which("rebar3")
    if original_getconf is None or rebar is None:
        raise RuntimeError("getconf and the selected mise rebar3 are required by the native build")
    jobs = os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL") or "2"
    if jobs not in {"1", "2"}:
        raise ValueError("CMAKE_BUILD_PARALLEL_LEVEL must be 1 or 2")
    build = ROOT / "_build"
    build.mkdir(exist_ok=True)
    with (build / "pybeam-build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another canonical native build is active") from error
        with tempfile.TemporaryDirectory(prefix="pybeam-build-tools-") as directory:
            getconf = Path(directory) / "getconf"
            getconf.write_text(
                '#!/bin/sh\nif [ "$1" = "_NPROCESSORS_ONLN" ]; then\n'
                "    printf '"
                + jobs
                + "\\n'\nelse\n    exec "
                + shlex.quote(original_getconf)
                + ' "$@"\nfi\n'
            )
            getconf.chmod(0o755)
            environment = dict(
                compiler_environment(),
                PATH=directory + os.pathsep + os.environ.get("PATH", os.defpath),
                PYTHON_CONFIG=str(config),
                CMAKE_BUILD_PARALLEL_LEVEL=jobs,
                IOMMAP_BUILDOPTS="-j " + jobs,
            )
            if platform.system() == "Darwin":
                environment["MACOSX_DEPLOYMENT_TARGET"] = "27.0"
            subprocess.run(  # noqa: S603
                [rebar, "as", "hornbeam", "compile"],
                cwd=ROOT,
                env=environment,
                check=True,
            )


if __name__ == "__main__":
    main()
