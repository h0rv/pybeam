import ast
import base64
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from examples.discord_simulation import Proof as DiscordProof
from pybeam.runtime.assets import (
    LinuxManifest,
    RuntimeAssets,
    RuntimeManifest,
    _host_python_library,
    discover_assets,
    packaged_assets,
)
from pybeam.runtime.config import BootstrapError, Config
from tools.build_runtime import (
    ELF,
    ERTS_EXECUTABLES,
    MACHO_MAGICS,
    MachO,
    OTPVersions,
    audit_binary,
    audit_elf,
    inspect_macho,
    linux_wheel_tag,
    manylinux_level,
    native_binaries,
    stage_hornbeam,
    stage_runtime,
    wheel_tag,
)
from tools.runtime_wheel import runtime_tag


class NativeState(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    pid: int
    package: Path
    pydantic: Path
    libraries: tuple[Path, ...]


@pytest.fixture
def linux_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    manifest = {
        "python_version": "3.14",
        "implementation": "cpython",
        "system": "Linux",
        "architecture": "x86_64",
        "wheel_tag": "cp314-cp314-linux_x86_64",
        "erts_version": "17.1.1",
        "executable": "erts-17.1.1/bin/erlexec",
        "bindir": "erts-17.1.1/bin",
        "root_dir": ".",
        "code_paths": ["lib/erlang_python-5.0.1/ebin"],
        "python_library": "libpython3.14.so.1.0",
    }
    (tmp_path / "runtime.json").write_text(json.dumps(manifest))
    for name in (
        *("erts-17.1.1/bin/" + item for item in ERTS_EXECUTABLES),
        "bin/start.boot",
        "lib/erlang_python-5.0.1/ebin/py_launch.beam",
        "lib/erlang_python-5.0.1/ebin/erlang_python.app",
        "lib/erlang_python-5.0.1/priv/py_nif.so",
        "host/lib/libpython3.14.so.1.0",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr("pybeam.runtime.assets.platform.system", lambda: "Linux")
    monkeypatch.setattr("pybeam.runtime.assets.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "host"))
    monkeypatch.setattr(
        "pybeam.runtime.assets.platform.mac_ver", lambda: pytest.fail("mac_ver on Linux")
    )
    return tmp_path


def test_linux_manifest_and_host_library(linux_assets: Path) -> None:
    manifest = RuntimeManifest.model_validate_json((linux_assets / "runtime.json").read_bytes())
    assert isinstance(manifest.root, LinuxManifest)
    assets = packaged_assets(linux_assets)
    assert assets.python_library == linux_assets / "host/lib/libpython3.14.so.1.0"
    assert assets.python_home == linux_assets / "host"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("architecture", "arm64"),
        ("system", "Darwin"),
        ("python_library", "libpython3.14.dylib"),
        ("wheel_tag", "cp314-cp314-macosx_15_0_arm64"),
        ("minimum_macos", [15, 0]),
    ],
)
def test_linux_manifest_rejects_mismatched_platform(
    linux_assets: Path, key: str, value: object
) -> None:
    file = linux_assets / "runtime.json"
    payload: object = json.loads(file.read_bytes())
    assert isinstance(payload, dict)
    payload[key] = value
    with pytest.raises(ValidationError):
        RuntimeManifest.model_validate_json(json.dumps(payload))


def test_linux_assets_missing_host_library(linux_assets: Path) -> None:
    (linux_assets / "host/lib/libpython3.14.so.1.0").unlink()
    with pytest.raises(BootstrapError, match="matching shared library"):
        packaged_assets(linux_assets)


@pytest.mark.parametrize("missing", ["bin/start.boot", "lib/erlang_python-5.0.1/priv/py_nif.so"])
def test_linux_assets_missing_native_file(linux_assets: Path, missing: str) -> None:
    (linux_assets / missing).unlink()
    with pytest.raises(BootstrapError, match="incomplete"):
        packaged_assets(linux_assets)


@pytest.mark.parametrize("existing", [None, "", "/other/lib"])
def test_linux_library_search_precedes_existing_environment(
    monkeypatch: pytest.MonkeyPatch,
    existing: str | None,
) -> None:
    monkeypatch.setattr("pybeam.runtime.assets.platform.system", lambda: "Linux")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    if existing is not None:
        monkeypatch.setenv("LD_LIBRARY_PATH", existing)
    environment = RuntimeAssets(
        Path("erl"),
        (),
        python_home=Path("/host"),
        python_library=Path("/host/lib/libpython3.14.so.1.0"),
    ).environment
    assert environment["PYTHONHOME"] == "/host"
    assert environment["LD_LIBRARY_PATH"] == "/host/lib" + (
        ":" + existing if existing is not None else ""
    )
    assert "DYLD_LIBRARY_PATH" not in environment


@pytest.mark.parametrize(
    ("machine", "dynamic", "python_nif", "error"),
    [
        ("AArch64", "", False, "x86-64"),
        ("Advanced Micro Devices X86-64", "", True, "CPython 3.14"),
        ("Advanced Micro Devices X86-64", "(NEEDED) [libevil.so]", False, "Unbundled"),
        ("Advanced Micro Devices X86-64", "(RUNPATH) [/build/lib]", False, "nonrelocatable"),
        ("Advanced Micro Devices X86-64", "(RPATH) [$ORIGIN/../lib]", False, "nonrelocatable"),
        ("Advanced Micro Devices X86-64", "(RUNPATH) []", False, "nonrelocatable"),
        ("Advanced Micro Devices X86-64", "(NEEDED) [libpython3.15.so.1.0]", True, "CPython 3.14"),
        (
            "Advanced Micro Devices X86-64",
            "(NEEDED) [libpython3.14.so.1.0]\n(NEEDED) [libpython3.14.so.1.0]",
            True,
            "CPython 3.14",
        ),
    ],
)
def test_elf_audit_rejects_unsafe_binaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    dynamic: str,
    python_nif: bool,
    error: str,
) -> None:
    binary = tmp_path / "native.so"
    binary.write_bytes(b"\x7fELF")
    monkeypatch.setattr(
        "tools.build_runtime.run", lambda args: "Machine: " + machine if "-h" in args else dynamic
    )
    with pytest.raises(ValueError, match=error):
        audit_elf(binary, python_nif=python_nif)


def test_elf_audit_and_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "native.so"
    binary.write_bytes(b"\x7fELF")
    monkeypatch.setattr(
        "tools.build_runtime.run",
        lambda args: (
            "Machine: Advanced Micro Devices X86-64"
            if "-h" in args
            else "(NEEDED) [libpython3.14.so.1.0]\n(NEEDED) [libc.so.6]"
        ),
    )
    assert audit_elf(binary, python_nif=True).rpaths == ()
    assert native_binaries(tmp_path, ()) == (binary,)
    binary.write_bytes(b"text")
    with pytest.raises(ValueError, match="not ELF"):
        audit_elf(binary)


def test_elf_audit__reads_highest_symbol_version_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "native.so"
    binary.write_bytes(b"\x7fELF")
    outputs = {
        "-h": "Machine: Advanced Micro Devices X86-64",
        "-d": "(NEEDED) [libc.so.6]",
        "-V": "Name: GLIBC_2.2.5 Flags: none\nName: GLIBC_2.34 Flags: none\nName: GLIBCXX_3.4.30",
    }
    monkeypatch.setattr("tools.build_runtime.run", lambda args: outputs[args[1]])
    assert audit_elf(binary).manylinux == (2, 35)


@pytest.mark.parametrize(
    ("versions", "level"),
    [
        ("GLIBC_2.2.5", (2, 17)),
        ("GLIBC_2.28 GLIBCXX_3.4.29", (2, 34)),
        ("GLIBC_2.35 GLIBCXX_3.4.21 CXXABI_1.3.9", (2, 35)),
        ("GLIBC_2.17 GCC_12.0.0", (2, 35)),
        ("GLIBC_2.28 CXXABI_1.3.15 CXXABI_TM_1", (2, 39)),
        ("GLIBC_2.17 CXXABI_1.3.16", None),
        ("GLIBC_2.17 GLIBCXX_3.4.34", None),
    ],
)
def test_manylinux_level__covers_libstdcxx_cxxabi_and_libgcc_symbols(
    versions: str, level: tuple[int, int] | None
) -> None:
    assert manylinux_level(versions) == level


@pytest.mark.parametrize(
    ("binaries", "tag"),
    [
        ((ELF(("libc.so.6",), ()),), "cp314-cp314-manylinux_2_17_x86_64"),
        (
            (
                ELF(("libpython3.14.so.1.0", "libc.so.6"), (), (2, 28)),
                ELF(("libz.so.1",), (), (2, 35)),
            ),
            "cp314-cp314-manylinux_2_35_x86_64",
        ),
        ((ELF(("libtinfo.so.6",), (), (2, 28)),), "cp314-cp314-linux_x86_64"),
        ((ELF(("libcrypto.so.3",), ()),), "cp314-cp314-linux_x86_64"),
        ((ELF(("libstdc++.so.6",), (), None),), "cp314-cp314-linux_x86_64"),
    ],
)
def test_linux_wheel_tag__claims_manylinux_only_for_portable_libraries(
    binaries: tuple[ELF, ...], tag: str
) -> None:
    assert linux_wheel_tag(binaries) == tag


def test_runtime_tag__requires_explicit_staged_assets(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Stage the bundled runtime"):
        runtime_tag(tmp_path)


def test_runtime_tag__rejects_false_portable_tag(tmp_path: Path) -> None:
    (tmp_path / "runtime.json").write_text(
        json.dumps(
            {
                "wheel_tag": "py3-none-any",
                "python_version": "3.14",
                "implementation": "cpython",
                "architecture": "arm64",
                "system": "Darwin",
            }
        )
    )
    with pytest.raises(ValueError, match="Unsupported wheel ABI"):
        runtime_tag(tmp_path)


def test_wheel_tag__does_not_round_down_macos_deployment_floor() -> None:
    assert wheel_tag((15, 0)) == "cp314-cp314-macosx_15_0_arm64"
    with pytest.raises(ValueError, match="MACOSX_DEPLOYMENT_TARGET"):
        wheel_tag((27, 2))


def test_inspect_macho__ignores_sdk_and_dylib_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def command(arguments: list[str], *, cwd: Path | None = None) -> str:
        if "lipo" in arguments[0]:
            return "arm64\n"
        if "-L" in arguments:
            return "binary:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        return (
            "cmd LC_BUILD_VERSION\n cmdsize 32\n platform 1\n minos 15.0\n sdk 27.2\n"
            "cmd LC_ID_DYLIB\n current version 27106.1.0\n"
        )

    monkeypatch.setattr("tools.build_runtime.run", command)
    binary = tmp_path / "binary"
    binary.write_bytes(bytes.fromhex("cffaedfe"))
    assert inspect_macho(binary).minimum == (15, 0)


@pytest.mark.parametrize(
    "dependencies",
    [("/opt/homebrew/lib/libssl.dylib",), ("@rpath/libpython3.15.dylib",)],
)
def test_native_audit__rejects_unbundled_or_wrong_python_libraries(
    dependencies: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def inspect(path: Path) -> MachO:
        return MachO(dependencies, (), (15, 0))

    monkeypatch.setattr("tools.build_runtime.inspect_macho", inspect)
    with pytest.raises(ValueError, match=r"CPython 3\.14"):
        audit_binary(tmp_path / "py_nif.so", python_nif=True)
    with pytest.raises(ValueError, match="Unbundled shared library"):
        audit_binary(tmp_path / "beam.smp")


@pytest.mark.parametrize("apple_platform", ["2", "3", "7", "6"])
def test_macho__rejects_non_macos_platform(
    apple_platform: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def command(arguments: list[str], *, cwd: Path | None = None) -> str:
        if "lipo" in arguments[0]:
            return "arm64\n"
        if "-L" in arguments:
            return "binary:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        return (
            "cmd LC_BUILD_VERSION\n cmdsize 32\n platform "
            + apple_platform
            + "\n minos 15.0\n sdk 27.2\n"
        )

    monkeypatch.setattr("tools.build_runtime.run", command)
    binary = tmp_path / "binary"
    binary.write_bytes(bytes.fromhex("cffaedfe"))
    with pytest.raises(ValueError, match="PLATFORM_MACOS"):
        inspect_macho(binary)


def test_macho__accepts_legacy_macos_deployment_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def command(arguments: list[str], *, cwd: Path | None = None) -> str:
        if "lipo" in arguments[0]:
            return "arm64\n"
        if "-L" in arguments:
            return "binary:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        return "cmd LC_VERSION_MIN_MACOSX\n cmdsize 16\n version 15.0\n sdk 27.2\n"

    monkeypatch.setattr("tools.build_runtime.run", command)
    binary = tmp_path / "binary"
    binary.write_bytes(bytes.fromhex("cffaedfe"))
    assert inspect_macho(binary).minimum == (15, 0)


@pytest.mark.parametrize("magic", sorted(MACHO_MAGICS))
def test_native_scan__recognizes_every_macho_container(magic: bytes, tmp_path: Path) -> None:
    binary = tmp_path / "unexpected-native-file"
    binary.write_bytes(magic)
    assert native_binaries(tmp_path, ()) == (binary,)


@pytest.mark.parametrize("executable", ERTS_EXECUTABLES)
def test_wheel_audit__rejects_universal_required_executables(
    executable: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets = tmp_path
    bindir = assets / "erts-17.1.1/bin"
    bindir.mkdir(parents=True)
    for name in ERTS_EXECUTABLES:
        (bindir / name).write_bytes(bytes.fromhex("cffaedfe"))
    (bindir / executable).write_bytes(bytes.fromhex("cafebabe"))
    app = assets / "lib/erlang_python-5.0.1"
    (app / "ebin").mkdir(parents=True)
    (app / "priv").mkdir()
    (app / "ebin/py_launch.beam").touch()
    (app / "priv/py_nif.so").write_bytes(bytes.fromhex("cffaedfe"))
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
        path = app / "priv" / name
        path.parent.mkdir(exist_ok=True)
        path.touch()
    for name in ("notices/OTP-LICENSE.txt", "notices/erlang-python-LICENSE", "bin/start.boot"):
        path = assets / name
        path.parent.mkdir(exist_ok=True)
        path.touch()
    manifest = {
        "python_version": "3.14",
        "implementation": "cpython",
        "architecture": "arm64",
        "system": "Darwin",
        "minimum_macos": [15, 0],
        "wheel_tag": "cp314-cp314-macosx_15_0_arm64",
        "erts_version": "17.1.1",
        "executable": "erts-17.1.1/bin/erlexec",
        "bindir": "erts-17.1.1/bin",
        "root_dir": ".",
        "code_paths": ["lib/erlang_python-5.0.1/ebin"],
        "python_library": "libpython3.14.dylib",
    }
    (assets / "runtime.json").write_text(json.dumps(manifest))
    real_audit = audit_binary

    def audit(path: Path, *, python_nif: bool = False) -> MachO:
        if path.name == executable:
            return real_audit(path, python_nif=python_nif)
        return MachO((), (), (15, 0))

    monkeypatch.setattr("tools.runtime_wheel.audit_binary", audit)
    with pytest.raises(ValueError, match="Universal Mach-O"):
        runtime_tag(assets)


def test_staging__resolves_inputs_before_systools_changes_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("tools.build_runtime.platform.system", lambda: "Darwin")
    monkeypatch.setattr("tools.build_runtime.platform.machine", lambda: "arm64")
    native = tmp_path / "native"
    (native / "ebin").mkdir(parents=True)
    (native / "ebin/py_launch.beam").touch()
    (native / "ebin/erlang_python.app").write_text('{application,erlang_python,[{vsn,"5.0.1"}]}.')
    license_file = tmp_path / "LICENSE"
    license_file.write_text("third-party license")
    monkeypatch.setattr(
        "tools.build_runtime.otp_information",
        lambda _erl: OTPVersions("17.1.1", "11.0", "8.0", "5.10"),
    )

    def command(arguments: list[str], *, cwd: Path | None = None) -> str:
        assert arguments[0] == str(tmp_path / "otp/bin/erl")
        assert str(native / "ebin") in arguments[-1]
        assert str(tmp_path / "otp") in arguments[-1]
        assert cwd is not None and cwd != tmp_path
        raise RuntimeError("captured absolute systools invocation")

    monkeypatch.setattr("tools.build_runtime.run", command)
    with pytest.raises(RuntimeError, match="captured absolute"):
        stage_runtime(Path("otp"), Path("native"), Path("LICENSE"), Path("LICENSE"), Path("output"))


def test_staging__hornbeam_services_include_runtime_code_and_notices(tmp_path: Path) -> None:
    source, output = tmp_path / "hornbeam", tmp_path / "runtime"
    (source / "ebin").mkdir(parents=True)
    (source / "priv/__pycache__").mkdir(parents=True)
    (source / "ebin/hornbeam.app").write_text('{application,hornbeam,[{vsn,"1.4.1"}]}.')
    (source / "ebin/hornbeam_services.beam").write_bytes(b"compiled-services")
    (source / "priv/worker.py").write_text("pass\n")
    (source / "priv/__pycache__/worker.pyc").write_bytes(b"cache")
    notice = source / "LICENSE"
    notice.write_text("third-party notice")
    result = stage_hornbeam(source, notice, output)
    assert result == Path("lib/hornbeam-1.4.1/ebin")
    assert (output / result / "hornbeam_services.beam").read_bytes() == b"compiled-services"
    assert (output / result.parent / "priv/worker.py").is_file()
    assert not (output / result.parent / "priv/__pycache__").exists()
    assert (output / "notices/hornbeam-LICENSE").read_text() == "third-party notice"


def test_staging__preserves_isolated_bootstrap_and_discards_nonruntime_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    otp, release, output = tmp_path / "otp", tmp_path / "release", tmp_path / "output"
    native = release / "lib/erlang_python-5.0.1"
    priv = native / "priv"
    runtime_files = {
        "_erlang_impl/__init__.py": b"from . import _server\n",
        "_erlang_impl/_server.py": b"from _erlang_isolated._server import serve\n",
        "_erlang_isolated/__init__.py": b"",
        "_erlang_isolated/_etf.py": b"# ETF codec\n",
        "_erlang_isolated/_isolated.py": b"# Child runtime\n",
        "_erlang_isolated/_server.py": b"# Shared server helpers\n",
        "_erlang_isolated/_shm.py": b"# Shared memory helpers\n",
        "py_isolated_child.py": b"from _erlang_isolated import _isolated\n",
    }
    files = {
        otp / "erts-17.1.1/bin/erl.src": b"%FINAL_ROOTDIR%",
        native / "ebin/erlang_python.app": b'{application,erlang_python,[{vsn,"5.0.1"}]}.',
        native / "ebin/py_launch.beam": b"beam",
        release / "releases/0.1.0/start.boot": b"boot",
        priv / "py_nif.so": b"native",
        priv / "test_helper.py": b"not runtime code",
        priv / "tests/test_helper.py": b"not runtime code",
        priv / "_erlang_isolated/__pycache__/_isolated.pyc": b"cache",
        tmp_path / "LICENSE": b"third-party notice",
        **{priv / name: content for name, content in runtime_files.items()},
        **{release / "erts-17.1.1/bin" / name: b"executable" for name in ERTS_EXECUTABLES},
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in release.iterdir():
            bundle.add(path, arcname=path.name)

    def command(_arguments: list[str], *, cwd: Path | None = None) -> str:
        if cwd is not None:
            shutil.copyfile(archive, cwd / "pybeam.tar.gz")
        return ""

    monkeypatch.setattr("tools.build_runtime.platform.system", lambda: "Linux")
    monkeypatch.setattr("tools.build_runtime.platform.machine", lambda: "x86_64")
    monkeypatch.setattr(
        "tools.build_runtime.otp_information",
        lambda _erl: OTPVersions("17.1.1", "11.0", "8.0", "5.10"),
    )
    monkeypatch.setattr("tools.build_runtime.run", command)
    monkeypatch.setattr("tools.build_runtime.audit_elf", lambda *_args, **_kwargs: ELF((), ()))
    notice = tmp_path / "LICENSE"
    stage_runtime(otp, native, notice, notice, output)
    staged_priv = output / "lib/erlang_python-5.0.1/priv"
    for name, content in runtime_files.items():
        assert (staged_priv / name).read_bytes() == content
    assert not (staged_priv / "test_helper.py").exists()
    assert not (staged_priv / "tests").exists()
    assert not (staged_priv / "_erlang_isolated/__pycache__").exists()


def test_packaged_assets__rejects_incompatible_host_before_native_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = {
        "python_version": "3.14",
        "implementation": "cpython",
        "architecture": "arm64",
        "system": "Darwin",
        "minimum_macos": [15, 0],
        "wheel_tag": "cp314-cp314-macosx_15_0_arm64",
        "erts_version": "17.1.1",
        "executable": "erts-17.1.1/bin/erlexec",
        "bindir": "erts-17.1.1/bin",
        "root_dir": ".",
        "code_paths": ["lib/erlang_python-5.0.1/ebin"],
        "python_library": "libpython3.14.dylib",
    }
    (tmp_path / "runtime.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("pybeam.runtime.assets.platform.machine", lambda: "x86_64")
    with pytest.raises(BootstrapError, match="not compatible"):
        packaged_assets(tmp_path)


def test_host_library__does_not_reuse_an_unrelated_compiled_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "relocated-python"
    old_libdir = tmp_path / "old-python/lib"
    old_libdir.mkdir(parents=True)
    (old_libdir / "libpython3.14.dylib").touch()
    monkeypatch.setattr("pybeam.runtime.assets.sys.base_prefix", str(home))
    monkeypatch.setattr(
        "pybeam.runtime.assets.sysconfig.get_config_var", lambda _name: str(old_libdir)
    )
    with pytest.raises(BootstrapError, match="matching shared library"):
        _host_python_library("libpython3.14.dylib")
    (home / "lib").mkdir(parents=True)
    expected = home / "lib/libpython3.14.dylib"
    expected.touch()
    assert _host_python_library("libpython3.14.dylib") == expected


def test_host_library__preserves_framework_symlink_basename_for_dyld(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "Python.framework/Versions/3.14"
    (home / "lib").mkdir(parents=True)
    (home / "Python").touch()
    library = home / "lib/libpython3.14.dylib"
    library.symlink_to("../Python")
    monkeypatch.setattr("pybeam.runtime.assets.sys.base_prefix", str(home))
    assert _host_python_library("libpython3.14.dylib") == library


def test_discovery__does_not_fall_back_to_an_unconfigured_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "src/pybeam/runtime/assets.py"
    source.parent.mkdir(parents=True)
    (tmp_path / "rebar.config").touch()
    (tmp_path / "_build/hornbeam").mkdir(parents=True)
    monkeypatch.setattr("pybeam.runtime.assets.__file__", str(source))
    with pytest.raises(BootstrapError, match="No supported managed runtime"):
        discover_assets(Config())


@pytest.mark.runtime
def test_installed_wheel__runs_from_moved_venv_without_developer_tools(tmp_path: Path) -> None:
    wheel_value = os.environ.get("PYBEAM_TEST_WHEEL")
    if wheel_value is None:
        pytest.skip("Set PYBEAM_TEST_WHEEL to the built platform wheel for the clean install gate")
    wheel = Path(wheel_value).resolve(strict=True)
    with ZipFile(wheel) as archive:
        dist_info = next(name for name in archive.namelist() if name.endswith(".dist-info/WHEEL"))
        metadata = archive.read(dist_info).decode()
        assert "Root-Is-Purelib: false" in metadata
        platform_tag = "(?:many)?linux_" if platform.system() == "Linux" else "macosx_"
        assert re.search("Tag: cp314-cp314-" + platform_tag, metadata)
        assert (archive.getinfo("pybeam/runtime/_assets/bin/erl").external_attr >> 16) & 0o111
        assert not any(
            name.startswith("hornbeam/") or "/lib/hornbeam-" in name for name in archive.namelist()
        )
        native_priv = "pybeam/runtime/_assets/lib/erlang_python-5.0.1/priv/"
        bindings = [
            name
            for name in archive.namelist()
            if name.startswith(native_priv) and name.endswith((".py", ".pyi"))
        ]
        assert any("erlang-stubs/actors.pyi" in name for name in bindings)
        assert any("_erlang_impl/_transport.py" in name for name in bindings)
        for name in bindings:
            assert archive.read(name.removeprefix(native_priv)) == archive.read(name)
        record = dist_info.removesuffix("WHEEL") + "RECORD"
        for name, digest, size in csv.reader(io.StringIO(archive.read(record).decode())):
            if name == record:
                assert digest == size == ""
            else:
                data = archive.read(name)
                encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
                assert digest == "sha256=" + encoded.decode()
                assert int(size) == len(data)
    uv = shutil.which("uv")
    assert uv is not None
    original = tmp_path / "original"
    host = tmp_path / "host-python"
    shutil.copytree(Path(sys.base_prefix), host, symlinks=True)
    interpreter = os.environ.get("PYBEAM_TEST_PYTHON", str(host / "bin/python3.14"))
    subprocess.run([uv, "venv", "--python", interpreter, str(original)], check=True)
    subprocess.run(
        [uv, "pip", "install", "--python", str(original / "bin/python"), str(wheel)],
        check=True,
    )
    moved = tmp_path / "moved"
    original.rename(moved)
    imports = subprocess.run(
        [
            str(moved / "bin/python"),
            "-c",
            "import asyncio, sys, erlang_services\n"
            "from pybeam.services.background import ServiceSlot\n"
            "assert 'erlang' not in sys.modules\n"
            "async def service():\n"
            "    pass\n"
            "async def verify():\n"
            "    try:\n"
            "        ServiceSlot(capacity=1).start(service)\n"
            "    except RuntimeError as error:\n"
            "        assert str(error) == 'services require a running ErlangEventLoop'\n"
            "    else:\n"
            "        raise AssertionError('ordinary asyncio admitted a native service')\n"
            "asyncio.run(verify())\n"
            "assert '_erlang_impl' not in sys.modules\n"
            "print(erlang_services.__file__)\n",
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert imports.returncode == 0, imports.stdout + imports.stderr
    assert str(moved / "lib/python3.14/site-packages/erlang_services.py") in imports.stdout
    installed = moved / "lib/python3.14/site-packages"
    assert (installed / "_erlang_impl/_actors.pyi").is_file()
    for stub in ("__init__.pyi", "actors.pyi", "supervision.pyi"):
        assert (installed / "erlang-stubs" / stub).is_file()
    assert not (installed / "erlang").exists()
    probe = tmp_path / "public_types.py"
    probe.write_text(
        "from typing import assert_type\n"
        "from erlang import ErlangEventLoop\n"
        "from erlang.actors import OwnedActor, DataActorRef, ActorInfo as NativeInfo\n"
        "from erlang.supervision import ChildState\n"
        "from pybeam import actor\n"
        "from pybeam.actors import ActorGroup, ActorInfo, ContextCounts, context_counts\n"
        "@actor\n"
        "class Counter:\n"
        "    async def echo(self, value: int, *, label: str) -> str:\n"
        "        return label * value\n"
        "async def method(state: object, value: int, *, label: str) -> str:\n"
        "    return label * value\n"
        "async def verify(actor: OwnedActor, ref: Counter, child: ChildState) -> None:\n"
        "    assert_type(await actor.call(0, method, 2, label='ok'), str)\n"
        "    assert_type(await ref.echo(2, label='ok'), str)\n"
        "    assert_type(child, ChildState)\n"
        "async def controls(group: ActorGroup, ref: Counter, native: DataActorRef) -> None:\n"
        "    assert_type(await group.info(ref), ActorInfo)\n"
        "    assert_type(await group.crash(ref), None)\n"
        "    assert_type(await context_counts(), ContextCounts)\n"
        "    assert_type(await native.info(), NativeInfo)\n"
        "    assert_type(await native.crash(), None)\n"
        "loop: ErlangEventLoop\n"
    )
    ty = shutil.which("ty")
    assert ty is not None
    typing = subprocess.run(
        [
            ty,
            "check",
            "--project",
            str(tmp_path),
            "--python",
            str(moved),
            "--error-on-warning",
            "--config",
            "rules.all='error'",
            str(probe),
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert typing.returncode == 0, typing.stdout + typing.stderr
    probe.write_text(probe.read_text().replace("2, label='ok'", "'wrong', label='ok'"))
    rejected = subprocess.run(
        [
            ty,
            "check",
            "--project",
            str(tmp_path),
            "--python",
            str(moved),
            "--error-on-warning",
            "--config",
            "rules.all='error'",
            str(probe),
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert rejected.returncode != 0
    assert rejected.stdout.count("error[invalid-argument-type]") == 2, rejected.stdout
    source_tests = Path(__file__).resolve().parent
    isolated_tests = tmp_path / "tests"
    (isolated_tests / "fixtures").mkdir(parents=True)
    for name in ("__init__.py", "fixtures/__init__.py"):
        (isolated_tests / name).touch()
    for name in ("pythonic_actors_positive.py", "fixtures/actor_inheritance.py"):
        shutil.copyfile(source_tests / name, isolated_tests / name)
    negative_source = (source_tests / "pythonic_actors_negative.txt").read_text()
    negative = isolated_tests / "negative.py"
    negative.write_text(negative_source)
    for target, expected in ((isolated_tests / "pythonic_actors_positive.py", 0), (negative, 1)):
        checked_types = subprocess.run(
            [
                ty,
                "check",
                "--project",
                str(tmp_path),
                "--python",
                str(moved),
                "--error-on-warning",
                "--config",
                "rules.all='error'",
                "--output-format",
                "concise",
                str(target),
            ],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = checked_types.stdout + checked_types.stderr
        assert checked_types.returncode == expected, output
        if expected == 1:
            assert "unresolved-import" not in output, output
            lines = [int(match[1]) for match in re.finditer(r":(\d+):\d+: error\[", output)]
            functions = [
                node
                for node in ast.parse(negative_source).body
                if isinstance(node, ast.AsyncFunctionDef)
            ]
            assert len(functions) == 18
            for function in functions:
                assert function.end_lineno is not None
                assert any(function.lineno <= line <= function.end_lineno for line in lines), output
    application = tmp_path / "application-original"
    application.mkdir()
    entry = application / "main.py"
    entry.write_text(
        "import sys, os, json\n"
        "from pathlib import Path\n"
        "import pybeam, pydantic\n"
        "from pybeam.actors import ActorGroup, actor, spawn\n"
        "@actor\n"
        "class Base:\n"
        "    value: int = 0\n"
        "    async def add(self, n: int) -> int:\n"
        "        self.value += n\n"
        "        return self.value\n"
        "@actor\n"
        "class Echo(Base):\n"
        "    async def echo(self, value: int) -> int:\n"
        "        return value\n"
        "async def main():\n"
        "    import erlang, ctypes\n"
        "    async with spawn(Echo, value=1) as reference:\n"
        "        assert await reference.echo(42) == 42\n"
        "        assert await reference.add(1) == 2\n"
        "        assert await Base.add(reference, 2) == 4\n"
        "    if sys.platform == 'linux':\n"
        "        libraries = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines() if 'libpython' in line})\n"
        "    else:\n"
        "        dyld = ctypes.CDLL(None)\n"
        "        dyld._dyld_image_count.restype = ctypes.c_uint\n"
        "        dyld._dyld_get_image_name.restype = ctypes.c_char_p\n"
        "        libraries = [dyld._dyld_get_image_name(i).decode() for i in range(dyld._dyld_image_count())]\n"
        "    print('NATIVE_OK', sys.version_info[:2], erlang.__name__)\n"
        "    print('NATIVE_STATE', json.dumps({'pid': os.getpid(), 'package': str(Path(pybeam.__file__).resolve()),\n"
        "        'pydantic': pydantic.__file__, 'libraries': [p for p in libraries if 'libpython' in p]}))\n"
        "pybeam.run(main)\n"
    )
    relocated = tmp_path / "application-moved"
    application.rename(relocated)
    application = relocated
    entry = application / "main.py"
    clean = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for command in ("erl", "escript", "rebar3", "mise"):
        assert shutil.which(command, path=clean["PATH"]) is None
    result = subprocess.run(
        [str(moved / "bin/python"), str(entry)],
        cwd=application,
        env=clean,
        capture_output=True,
        check=False,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("NATIVE_OK") == 1, result.stdout
    assert "(3, 14) erlang" in result.stdout
    assert str(moved / "lib/python3.14/site-packages/pybeam/__init__.py") in result.stdout
    library = (
        host
        / "lib"
        / ("libpython3.14.so.1.0" if platform.system() == "Linux" else "libpython3.14.dylib")
    )
    assert str(library) in result.stdout
    assert "/Users/robby/projects" not in result.stdout + result.stderr
    assert "/mise/" not in result.stdout + result.stderr
    state_line = next(
        line for line in result.stdout.splitlines() if line.startswith("NATIVE_STATE ")
    )
    state = NativeState.model_validate_json(state_line.removeprefix("NATIVE_STATE "))
    assert state.libraries == (library,)
    assert state.package == moved / "lib/python3.14/site-packages/pybeam/__init__.py"
    assert state.pydantic == moved / "lib/python3.14/site-packages/pydantic/__init__.py"
    with pytest.raises(ProcessLookupError):
        os.kill(state.pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(state.pid, 0)
    console = tmp_path / "console"
    subprocess.run([uv, "venv", "--python", interpreter, str(console)], check=True)
    subprocess.run(
        [uv, "pip", "install", "--python", str(console / "bin/python"), str(wheel)], check=True
    )
    cli = subprocess.run(
        [str(console / "bin/pybeam"), "run", str(entry)],
        cwd=application,
        env=clean,
        capture_output=True,
        check=False,
        text=True,
        timeout=45,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert cli.stdout.count("NATIVE_OK") == 1
    assert str(console / "lib/python3.14/site-packages/pybeam/__init__.py") in cli.stdout
    assert str(library) in cli.stdout
    assert "/Users/robby/projects" not in cli.stdout + cli.stderr
    assert "/mise/" not in cli.stdout + cli.stderr
    cli_state_line = next(
        line for line in cli.stdout.splitlines() if line.startswith("NATIVE_STATE ")
    )
    cli_state = NativeState.model_validate_json(cli_state_line.removeprefix("NATIVE_STATE "))
    assert cli_state.libraries == (library,)
    root = Path(__file__).resolve().parents[1]
    for source, marker in (
        (root / "examples/actors/main.py", "[1, 1, 1, 1, 1]"),
        (root / "tests/fixtures/actor_inheritance.py", "pythonic-inheritance-ok"),
        (root / "examples/discord_simulation.py", '"fanout_receipts_before_fault":800'),
    ):
        relocated_probe = application / (source.stem + "_probe.py")
        shutil.copyfile(source, relocated_probe)
        checked = subprocess.run(
            [str(moved / "bin/python"), str(relocated_probe)],
            cwd=application,
            env=clean,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert marker in checked.stdout
        assert "/Users/robby/projects" not in checked.stdout + checked.stderr
        if source.name == "discord_simulation.py":
            proof = next(
                DiscordProof.model_validate_json(line)
                for line in checked.stdout.splitlines()
                if line.startswith("{")
            )
            assert len({info.pid for info in proof.room_identities}) == 4
            assert proof.fanout_receipts_before_fault == 800
            assert proof.before.pid != proof.after.pid
            assert proof.before.generation != proof.after.generation
            assert proof.fresh.members == () and proof.fresh.event_ids == ()
            assert proof.contexts_before.live == proof.contexts_after.live
    with pytest.raises(ProcessLookupError):
        os.killpg(cli_state.pid, 0)
    manifest = moved / "lib/python3.14/site-packages/pybeam/runtime/_assets/runtime.json"
    saved = manifest.read_text()
    payload: object = json.loads(saved)
    assert isinstance(payload, dict)
    payload["python_version"] = "3.15"
    manifest.write_text(json.dumps(payload))
    incompatible = subprocess.run(
        [str(moved / "bin/python"), str(entry)],
        cwd=application,
        env=clean,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert incompatible.returncode != 0
    assert "not compatible" in incompatible.stderr
    assert "NATIVE_OK" not in incompatible.stdout
    manifest.write_text(saved)
    nif = next(manifest.parent.glob("lib/erlang_python-*/priv/py_nif.so"))
    hidden_nif = nif.with_suffix(".missing")
    nif.rename(hidden_nif)
    missing_nif = subprocess.run(
        [str(moved / "bin/python"), str(entry)],
        cwd=application,
        env=clean,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert missing_nif.returncode != 0
    assert "incomplete" in missing_nif.stderr
    assert "NATIVE_OK" not in missing_nif.stdout
    hidden_nif.rename(nif)
    manifest.unlink()
    missing = subprocess.run(
        [str(moved / "bin/python"), str(entry)],
        cwd=application,
        env=clean,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert missing.returncode != 0
    assert "NATIVE_OK" not in missing.stdout
    assert "runtime" in missing.stderr.lower()
