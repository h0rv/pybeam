import json
import re
from pathlib import Path

from pybeam.runtime.optional_manifest import verify_manifest
from tools.build_runtime import ERTS_EXECUTABLES, audit_binary, native_binaries, wheel_tag


def audit_optional(root: Path) -> None:
    verify_manifest(root)


def runtime_tag(assets: Path) -> str:
    try:
        manifest: object = json.loads((assets / "runtime.json").read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(
            "Stage the bundled runtime with tools/build_runtime.py before building"
        ) from error
    if not isinstance(manifest, dict):
        raise ValueError("Invalid runtime manifest")
    if (
        manifest.get("python_version") != "3.14"
        or manifest.get("implementation") != "cpython"
        or manifest.get("architecture") != "arm64"
        or manifest.get("system") != "Darwin"
    ):
        raise ValueError("Unsupported runtime ABI/platform")
    tag: object = manifest.get("wheel_tag")
    if not isinstance(tag, str) or not re.fullmatch(r"cp314-cp314-macosx_\d+_\d+_arm64", tag):
        raise ValueError("Unsupported wheel ABI/platform tag")
    erts: object = manifest.get("erts_version")
    if not isinstance(erts, str) or not re.fullmatch(r"[0-9.]+", erts):
        raise ValueError("Invalid ERTS version")
    bindir = "erts-" + erts + "/bin"
    if (
        manifest.get("executable") != bindir + "/erlexec"
        or manifest.get("bindir") != bindir
        or manifest.get("root_dir") != "."
    ):
        raise ValueError("Runtime must use native relocatable OTP erlexec")
    for name in ERTS_EXECUTABLES:
        if not (assets / bindir / name).is_file():
            raise ValueError("Missing bundled ERTS executable: " + name)
    natives = tuple(assets.glob("lib/erlang_python-*/priv/py_nif.so"))
    launchers = tuple(assets.glob("lib/erlang_python-*/ebin/py_launch.beam"))
    if len(natives) != 1 or len(launchers) != 1:
        raise ValueError("Missing or ambiguous canonical native runtime")
    for required in ("notices/OTP-LICENSE.txt", "notices/erlang-python-LICENSE", "bin/start.boot"):
        if not (assets / required).is_file():
            raise ValueError("Missing runtime asset: " + required)
    priv = natives[0].parent
    for name in (
        "_erlang_impl/__init__.py",
        "_erlang_impl/_actors.py",
        "_erlang_impl/_actors.pyi",
        "erlang-stubs/__init__.pyi",
        "erlang-stubs/actors.pyi",
        "erlang-stubs/supervision.pyi",
        "erlang_services.py",
        "py_isolated_child.py",
    ):
        if not (priv / name).is_file():
            raise ValueError("Missing native Python binding: " + name)
    pieces = tag.split("macosx_", 1)[1].split("_")
    target = (int(pieces[0]), int(pieces[1]))
    if wheel_tag(target) != tag:
        raise ValueError("Runtime deployment target does not match wheel tag")
    for path in assets.rglob("*"):
        if path.is_symlink():
            raise ValueError("Runtime assets must not contain symlinks: " + str(path))
    required = (natives[0], *(assets / bindir / name for name in ERTS_EXECUTABLES))
    for path in native_binaries(assets, required):
        info = audit_binary(path, python_nif=path == natives[0])
        if info.minimum > target:
            raise ValueError("Wheel tag understates the native macOS deployment target")
        if any(not item.startswith("@loader_path") for item in info.rpaths):
            raise ValueError("Runtime contains a nonrelocatable dylib search path")
    audit_optional(assets / "optional/genstage")
    return tag
