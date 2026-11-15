import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

from pybeam.runtime.optional import OptionalManifest

ROOT = Path(__file__).resolve().parents[1]


def run(python: Path, entry: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), str(entry)],
        cwd=entry.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def shutdown(python: Path, entry: Path, environment: dict[str, str]) -> None:
    with subprocess.Popen(
        [str(python), str(entry)],
        cwd=entry.parent,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    ) as child:
        assert child.stdout is not None
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 10
                while b"SHUTDOWN_READY" not in output:
                    assert time.monotonic() < deadline, output.decode()
                    assert selector.select(timeout=1), output.decode()
                    chunk = os.read(child.stdout.fileno(), 4096)
                    assert chunk, output.decode()
                    output.extend(chunk)
            child.send_signal(signal.SIGTERM)
            tail, _ = child.communicate(timeout=10)
            output.extend(tail)
            assert child.returncode == 143, output.decode()
            assert b"SHUTDOWN_CONTEXTS 1" in output, output.decode()
            assert b"cleanup failed" not in output, output.decode()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


@pytest.mark.runtime
def test_installed_genstage_without_developer_environment(tmp_path: Path) -> None:
    value = os.environ.get("PYBEAM_TEST_WHEEL")
    if value is None:
        pytest.skip("Set PYBEAM_TEST_WHEEL to the privately built platform wheel")
    wheel = Path(value).resolve(strict=True)
    with ZipFile(wheel) as archive:
        manifest_path = "pybeam/runtime/_assets/optional/genstage/manifest.json"
        manifest = OptionalManifest.model_validate_json(archive.read(manifest_path))
        assert manifest.otp_version == "29.1.2"
        assert {app.name for app in manifest.applications} == {
            "compiler",
            "elixir",
            "logger",
            "gen_stage",
            "pybeam_genstage",
        }
        for item in manifest.files:
            content = archive.read(str(Path(manifest_path).parent / item.path))
            assert hashlib.sha256(content).hexdigest() == item.sha256
        assert len([name for name in archive.namelist() if name.endswith("/bin/erlexec")]) == 1
        assert len([name for name in archive.namelist() if name.endswith("/priv/py_nif.so")]) == 1
        assert not any("libpython" in name for name in archive.namelist())
        assert not any(name.endswith((".ex", ".erl")) for name in archive.namelist())
        core: object = json.loads(archive.read("pybeam/runtime/_assets/runtime.json"))
        assert isinstance(core, dict)
        paths: object = core.get("code_paths")
        assert isinstance(paths, list)
        assert all(isinstance(path, str) and "optional" not in path for path in paths)
    uv = shutil.which("uv")
    assert uv is not None
    host = tmp_path / "host-python"
    shutil.copytree(Path(sys.base_prefix), host, symlinks=True)
    project = tmp_path / "consumer-project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname="genstage-consumer"\nversion="0.0.0"\n'
        'requires-python=">=3.14"\ndependencies=[]\n'
    )
    original = project / ".venv"
    subprocess.run(
        [uv, "venv", "--python", str(host / "bin/python3.14"), str(original)], check=True
    )
    subprocess.run(
        [
            uv,
            "add",
            "--project",
            str(project),
            "--python",
            str(host / "bin/python3.14"),
            "python-beam[genstage] @ " + wheel.as_uri(),
        ],
        check=True,
    )
    moved = tmp_path / "installed"
    original.rename(moved)
    python = moved / "bin/python"
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for name in ("erl", "escript", "rebar3", "mise", "elixir", "mix"):
        assert shutil.which(name, path=environment["PATH"]) is None
    application = tmp_path / "application"
    application.mkdir()
    for source in (
        ROOT / "examples/genstage.py",
        ROOT / "examples/genstage_callbacks.py",
        ROOT / "examples/actors/main.py",
        ROOT / "tests/native/genstage_entry.py",
        ROOT / "tests/native/genstage_owned.py",
        ROOT / "tests/native/genstage_shutdown.py",
        ROOT / "tests/native/genstage_wheel.py",
    ):
        shutil.copyfile(source, application / source.name)
    probe = ROOT / ".experiment/genstage-probe/ebin"
    assert probe.is_dir(), "Compile the test-only fixture with mise run experiment:genstage-probe"
    shutil.copytree(probe, application / "probe/ebin")
    imports = subprocess.run(
        [
            str(python),
            "-c",
            "import sys, pybeam, erlang_services; from pybeam.services.background import ServiceSlot; assert 'erlang' not in sys.modules; assert 'pybeam.integrations.genstage' not in sys.modules",
        ],
        cwd=application,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert imports.returncode == 0, imports.stdout + imports.stderr
    for name, marker in (
        ("genstage.py", "GenStage transformed 16 ordered items in 16 single-credit batches"),
        ("genstage_entry.py", "drained native contexts to baseline 1"),
        ("genstage_wheel.py", "INSTALLED_PROOF"),
    ):
        result = run(python, application / name, environment)
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker in result.stdout, result.stdout
        assert "/Users/robby/projects" not in result.stdout + result.stderr
        assert "/mise/" not in result.stdout + result.stderr
        if name == "genstage_wheel.py":
            print(result.stdout)
    shutdown(python, application / "genstage_shutdown.py", environment)
    optional = moved / "lib/python3.14/site-packages/pybeam/runtime/_assets/optional/genstage"
    hidden = optional.with_name("hidden")
    optional.rename(hidden)
    core_only = run(python, application / "main.py", environment)
    assert core_only.returncode == 0, core_only.stdout + core_only.stderr
    unavailable = run(python, application / "genstage.py", environment)
    assert unavailable.returncode != 0
    assert "Optional runtime artifact is unavailable" in unavailable.stderr
    hidden.rename(optional)
    module = next(optional.glob("lib/gen_stage-*/ebin/Elixir.GenStage.beam"))
    saved = module.read_bytes()
    module.write_bytes(saved + b"tampered")
    rejected = run(python, application / "genstage.py", environment)
    assert rejected.returncode != 0
    assert "Optional runtime artifact is unavailable" in rejected.stderr
    module.write_bytes(saved)
    print("INSTALLED_DIRECTORY", moved)
    print("INSTALLED_WHEEL", wheel)
