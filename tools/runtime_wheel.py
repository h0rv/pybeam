import json
import re
from pathlib import Path

from pybeam.runtime.assets import MANIFEST
from pybeam.runtime.optional_manifest import verify_manifest
from tools.build_runtime import (
    ERTS_EXECUTABLES,
    audit_binary,
    audit_elf,
    native_binaries,
    wheel_tag,
)


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
    linux = manifest.get("system") == "Linux" and manifest.get("architecture") == "x86_64"
    if (
        manifest.get("python_version") != "3.14"
        or manifest.get("implementation") != "cpython"
        or (
            not linux
            and (manifest.get("architecture") != "arm64" or manifest.get("system") != "Darwin")
        )
    ):
        raise ValueError("Unsupported runtime ABI/platform")
    tag: object = manifest.get("wheel_tag")
    pattern = r"cp314-cp314-linux_x86_64" if linux else r"cp314-cp314-macosx_\d+_\d+_arm64"
    if not isinstance(tag, str) or not re.fullmatch(pattern, tag):
        raise ValueError("Unsupported wheel ABI/platform tag")
    MANIFEST.decode((assets / "runtime.json").read_bytes())
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
    distributed = bool(tuple(assets.glob("lib/erlang_python-*/ebin/py_distribution.beam")))
    executables = tuple(name for name in ERTS_EXECUTABLES if name != "epmd" or distributed)
    for name in executables:
        if not (assets / bindir / name).is_file():
            raise ValueError("Missing bundled ERTS executable: " + name)
    natives = tuple(assets.glob("lib/erlang_python-*/priv/py_nif.so"))
    launchers = tuple(assets.glob("lib/erlang_python-*/ebin/py_launch.beam"))
    if len(natives) != 1 or len(launchers) != 1:
        raise ValueError("Missing or ambiguous canonical native runtime")
    for required in ("notices/OTP-LICENSE.txt", "notices/erlang-python-LICENSE", "bin/start.boot"):
        if not (assets / required).is_file():
            raise ValueError("Missing runtime asset: " + required)
    if distributed:
        crypto = tuple(assets.glob("lib/crypto-*/priv/lib/crypto.so"))
        if len(crypto) != 1 or not (crypto[0].parents[2] / "ebin/crypto.beam").is_file():
            raise ValueError("Missing or ambiguous OTP crypto runtime")
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
    target = (0, 0)
    if not linux:
        pieces = tag.split("macosx_", 1)[1].split("_")
        target = (int(pieces[0]), int(pieces[1]))
        if wheel_tag(target) != tag:
            raise ValueError("Runtime deployment target does not match wheel tag")
    for path in assets.rglob("*"):
        if path.is_symlink():
            raise ValueError("Runtime assets must not contain symlinks: " + str(path))
    required = (natives[0], *(assets / bindir / name for name in executables))
    for path in native_binaries(assets, required):
        if linux:
            audit_elf(path, python_nif=path == natives[0])
            continue
        info = audit_binary(path, python_nif=path == natives[0])
        if info.minimum > target:
            raise ValueError("Wheel tag understates the native macOS deployment target")
        if any(not item.startswith("@loader_path") for item in info.rpaths):
            raise ValueError("Runtime contains a nonrelocatable dylib search path")
    optional_payload(assets, linux=linux)
    return tag


def optional_payload(assets: Path, *, linux: bool) -> None:
    root = assets / "optional/genstage"
    if linux:
        if root.exists():
            raise ValueError("Linux runtime wheels must not bundle the GenStage payload")
    else:
        audit_optional(root)
