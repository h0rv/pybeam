import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from pybeam.runtime.assets import packaged_assets
from pybeam.runtime.config import BootstrapError
from tools.build_runtime import (
    ERTS_EXECUTABLES,
    MACHO_MAGICS,
    MachO,
    OTPVersions,
    audit_binary,
    inspect_macho,
    native_binaries,
    stage_runtime,
    wheel_tag,
)
from tools.runtime_wheel import runtime_tag


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
    native = tmp_path / "native"
    (native / "ebin").mkdir(parents=True)
    (native / "ebin/py_launch.beam").touch()
    (native / "ebin/erlang_python.app").write_text('{application,erlang_python,[{vsn,"5.0.1"}]}.')
    license_file = tmp_path / "LICENSE"
    license_file.write_text("third-party license")
    monkeypatch.setattr(
        "tools.build_runtime.otp_information", lambda _erl: OTPVersions("17.1.1", "11.0", "8.0")
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


def test_packaged_assets__rejects_incompatible_host_before_native_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = {
        "python_version": "3.14", "implementation": "cpython", "architecture": "arm64",
        "system": "Darwin", "minimum_macos": [15, 0],
        "wheel_tag": "cp314-cp314-macosx_15_0_arm64", "erts_version": "17.1.1",
        "executable": "erts-17.1.1/bin/erlexec", "bindir": "erts-17.1.1/bin", "root_dir": ".",
        "code_paths": ["lib/erlang_python-5.0.1/ebin"], "python_library": "libpython3.14.dylib",
    }
    (tmp_path / "runtime.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("pybeam.runtime.assets.platform.machine", lambda: "x86_64")
    with pytest.raises(BootstrapError, match="not compatible"):
        packaged_assets(tmp_path)


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
        assert "Tag: cp314-cp314-macosx_" in metadata
        assert (archive.getinfo("pybeam/runtime/_assets/bin/erl").external_attr >> 16) & 0o111
        assert not any("hornbeam" in name.lower() for name in archive.namelist())
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
    application = tmp_path / "application-original"
    application.mkdir()
    entry = application / "main.py"
    entry.write_text(
        "import sys, os, json\n"
        "from pathlib import Path\n"
        "import pybeam, pydantic\n"
        "from pybeam.actors import ActorSpec, spawn\n"
        "from pybeam.mailbox import UnboundedMailbox\n"
        "class Echo:\n"
        "    async def echo(self, value: int) -> int:\n"
        "        return value\n"
        "async def main():\n"
        "    import erlang, ctypes\n"
        "    async with await spawn(ActorSpec(Echo, (Echo.echo,)), mailbox=UnboundedMailbox()) as actor:\n"
        "        assert await actor.call(Echo.echo, 42) == 42\n"
        "    dyld = ctypes.CDLL(None)\n"
        "    dyld._dyld_image_count.restype = ctypes.c_uint\n"
        "    dyld._dyld_get_image_name.restype = ctypes.c_char_p\n"
        "    libraries = [dyld._dyld_get_image_name(i).decode() for i in range(dyld._dyld_image_count())]\n"
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
    assert str(host / "lib/libpython3.14.dylib") in result.stdout
    assert "/Users/robby/projects" not in result.stdout + result.stderr
    assert "/mise/" not in result.stdout + result.stderr
    state_line = next(
        line for line in result.stdout.splitlines() if line.startswith("NATIVE_STATE ")
    )
    state: object = json.loads(state_line.removeprefix("NATIVE_STATE "))
    assert isinstance(state, dict)
    pid: object = state["pid"]
    assert isinstance(pid, int)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    cli = subprocess.run(
        [str(moved / "bin/python"), str(moved / "bin/pybeam"), "run", str(entry)],
        cwd=application,
        env=clean,
        capture_output=True,
        check=False,
        text=True,
        timeout=45,
    )
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert cli.stdout.count("NATIVE_OK") == 1
    assert str(host / "lib/libpython3.14.dylib") in cli.stdout
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
        cwd=application, env=clean, capture_output=True, check=False, text=True, timeout=15,
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
