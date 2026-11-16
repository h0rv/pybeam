import hashlib
import json
import os
import platform
import selectors
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from pybeam.runtime.config import BootstrapError
from pybeam.runtime.optional import OptionalManifest, read_manifest
from tests.readiness import ProcessOutput, wait_ready
from tools.genstage_assets import approved_artifact, digest, stage
from tools.genstage_fixtures import prepare
from tools.runtime_wheel import audit_optional

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


def shutdown(
    python: Path,
    entry: Path,
    environment: dict[str, str],
    *,
    before_activation: bool = False,
) -> None:
    with subprocess.Popen(
        [str(python), str(entry), *(["--before-activation"] if before_activation else [])],
        cwd=entry.parent,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    ) as child:
        assert child.stdout is not None
        identity = subprocess.check_output(
            ["/bin/ps", "-p", str(child.pid), "-o", "pid=,lstart=,command="], text=True
        )
        print("OWN_SHUTDOWN_PROCESS", identity.strip())
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                wait_ready(
                    ProcessOutput(child, selector),
                    output,
                    ready_line=b"BEFORE_ACTIVATION_READY\n"
                    if before_activation
                    else b"SHUTDOWN_READY\n",
                )
            child.send_signal(signal.SIGTERM)
            tail, _ = child.communicate(timeout=10)
            output.extend(tail)
            assert child.returncode == 143, output.decode()
            assert b"SHUTDOWN_CONTEXTS 1" in output, output.decode()
            assert b"COUNTER_ACK 42" in output, output.decode()
            assert b"PHASE owner_context_drained 1" in output, output.decode()
            if before_activation:
                assert b"PHASE stop phase=activation_pending" in output, output.decode()
                assert b"PHASE graph_created" not in output, output.decode()
                assert b"PHASE application_registered" not in output, output.decode()
            else:
                assert b"PHASE manifest_verified" in output, output.decode()
                assert b"PHASE application_registration_acknowledged" in output, output.decode()
                assert b"PHASE stop phase=graph_owned" in output, output.decode()
            assert b"Function not registered" not in output, output.decode()
            assert b"cleanup failed" not in output, output.decode()
            print(output.decode(errors="replace"))
        except BaseException as failure:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
            try:
                tail, _ = child.communicate(timeout=10)
                output.extend(tail)
            except subprocess.TimeoutExpired as cleanup:
                child.kill()
                tail, _ = child.communicate(timeout=5)
                output.extend(tail)
                failure.add_note("Owned cleanup timed out: " + str(cleanup))
            failure.add_note("OWNED_CLEANUP_OUTPUT\n" + output.decode(errors="replace"))
            print(output.decode(errors="replace"))
            raise


@pytest.mark.runtime
def test_installed_genstage_without_developer_environment(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if not request.config.getoption("run_runtime"):
        pytest.skip("Requires --run-runtime and PYBEAM_TEST_WHEEL")
    value = os.environ.get("PYBEAM_TEST_WHEEL")
    assert value is not None, "Set PYBEAM_TEST_WHEEL to the current private platform wheel"
    wheel = Path(value).resolve(strict=True)
    with ZipFile(wheel) as archive:
        manifest_path = "pybeam/runtime/_assets/optional/genstage/manifest.json"
        manifest = OptionalManifest.model_validate_json(archive.read(manifest_path))
        approved = OptionalManifest.model_validate_json(
            archive.read(
                "pybeam/runtime/optional_specs/genstage"
                + ("-linux_x86_64" if platform.system() == "Linux" else "")
                + ".json"
            )
        )
        assert manifest == approved
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
        helper = next(
            name for name in archive.namelist() if name.endswith("/ebin/py_application.beam")
        )
        assert hashlib.sha256(archive.read(helper)).hexdigest() == approved_artifact().helper
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
            "--offline",
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
        ROOT / "examples/discord_simulation.py",
        ROOT / "tests/native/genstage_coexist.py",
        ROOT / "tests/native/genstage_entry.py",
        ROOT / "tests/native/genstage_owned.py",
        ROOT / "tests/native/genstage_shutdown.py",
        ROOT / "tests/native/genstage_wheel.py",
    ):
        shutil.copyfile(source, application / source.name)
    fixtures = tmp_path / "fixtures"
    prepare(wheel, fixtures)
    staged_nif = fixtures / "assets/runtime/lib/erlang_python-5.0.1/priv/py_nif.so"
    identity, checksum = staged_nif.stat().st_ino, digest(staged_nif)
    with pytest.raises(FileExistsError, match="new output directory"):
        stage(wheel, fixtures / "assets")
    assert (staged_nif.stat().st_ino, digest(staged_nif)) == (identity, checksum)
    shutil.copytree(fixtures / "probe", application / "probe")
    shutil.copytree(fixtures / "compat", application / "compat")
    imports = subprocess.run(
        [
            str(python),
            "-c",
            "import sys, pybeam, erlang_services; from pybeam.services.background import ServiceSlot; assert 'erlang' not in sys.modules; assert 'pybeam.integrations.genstage' not in sys.modules; assert 'pybeam.runtime.optional_manifest' not in sys.modules; assert not {'fastapi', 'litestar', 'pyview', 'hornbeam'} & sys.modules.keys()",
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
            assert "COMPATIBILITY_PREFLIGHT_OK" in result.stdout
            assert "CURRENT_CORE_COEXISTENCE" in result.stdout
            print(result.stdout)
    shutdown(python, application / "genstage_shutdown.py", environment)
    shutdown(python, application / "genstage_shutdown.py", environment, before_activation=True)
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
    manifest_file = optional / "manifest.json"
    saved_manifest = manifest_file.read_bytes()

    def invalid_manifest_is_rejected() -> None:
        with pytest.raises(BootstrapError, match="Optional runtime artifact"):
            read_manifest(optional)
        with pytest.raises((ValueError, OSError)):
            audit_optional(optional)
        result = run(python, application / "genstage.py", environment)
        assert result.returncode != 0
        assert "Optional runtime artifact is unavailable" in result.stderr
        assert "GenStage transformed" not in result.stdout

    for field, value in (
        ("otp_version", "0.0.0"),
        ("sources", {"elixir": "0" * 40, "gen_stage": "0" * 40}),
    ):
        document: object = json.loads(saved_manifest)
        assert isinstance(document, dict)
        document[field] = value
        try:
            manifest_file.write_text(json.dumps(document))
            invalid_manifest_is_rejected()
        finally:
            manifest_file.write_bytes(saved_manifest)
    extra = module.with_name("unapproved.beam")
    extra.write_bytes(b"unapproved but hash-declared code")
    document = json.loads(saved_manifest)
    assert isinstance(document, dict)
    files: object = document["files"]
    assert isinstance(files, list)
    files.append(
        {
            "path": str(extra.relative_to(optional)),
            "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
        }
    )
    try:
        manifest_file.write_text(json.dumps(document))
        invalid_manifest_is_rejected()
    finally:
        extra.unlink()
        manifest_file.write_bytes(saved_manifest)
    external = tmp_path / "external-manifest.json"
    external.write_bytes(saved_manifest)
    manifest_file.unlink()
    manifest_file.symlink_to(external)
    try:
        invalid_manifest_is_rejected()
    finally:
        manifest_file.unlink()
        manifest_file.write_bytes(saved_manifest)
    print("INSTALLED_MANIFEST_NEGATIVES source/OTP/extra-beam/external-manifest rejected")
    print("INSTALLED_DIRECTORY", moved)
    print("INSTALLED_WHEEL", wheel)
