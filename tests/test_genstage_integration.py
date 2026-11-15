import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from examples.genstage_callbacks import process
from pybeam.integrations.genstage.pipeline import stream
from pybeam.runtime.config import BootstrapError
from pybeam.runtime.optional import read_manifest, start_optional
from tools.runtime_wheel import audit_optional


def artifact(root: Path) -> Path:
    directory = root / "lib/example/ebin"
    directory.mkdir(parents=True)
    app = directory / "example.app"
    app.write_text('{application, example, [{vsn, "1.0"}]}.')
    beam = directory / "example.beam"
    beam.write_bytes(b"test fixture")
    files = [
        {
            "path": str(path.relative_to(root)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (app, beam)
    ]
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "name": "example",
                "otp_version": "29.1.2",
                "erts_version": "17.1.1",
                "sources": {
                    "elixir": "853b7cd719bce7df87be4271ee91c90acdf706e1",
                    "gen_stage": "d1532fa56482bc92d90abc777439ff8be34b5b1a",
                },
                "applications": [
                    {"name": "example", "version": "1.0", "code_path": "lib/example/ebin"}
                ],
                "files": files,
            }
        )
    )
    return root


@pytest.mark.asyncio
async def test_optional_activation_requires_the_managed_entry_loop() -> None:
    with pytest.raises(BootstrapError, match="require a running managed Python entry"):
        await start_optional("genstage")


def test_missing_optional_artifact(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="Optional runtime artifact is unavailable"):
        read_manifest(tmp_path)


def test_manifest_verifies_all_files(tmp_path: Path) -> None:
    root = artifact(tmp_path)
    assert read_manifest(root).name == "example"
    audit_optional(root)
    (root / "lib/example/ebin/example.beam").write_bytes(b"modified")
    with pytest.raises(BootstrapError, match="Optional runtime artifact"):
        read_manifest(root)
    with pytest.raises(ValueError, match="hash mismatch"):
        audit_optional(root)


def test_undeclared_code_fails_closed(tmp_path: Path) -> None:
    root = artifact(tmp_path)
    (root / "lib/example/ebin/unknown.beam").touch()
    with pytest.raises(BootstrapError):
        read_manifest(root)
    with pytest.raises(ValueError, match="undeclared"):
        audit_optional(root)


def test_symlinked_code_fails_closed(tmp_path: Path) -> None:
    root = artifact(tmp_path)
    beam = root / "lib/example/ebin/example.beam"
    saved = beam.read_bytes()
    beam.unlink()
    other = root / "elsewhere"
    other.write_bytes(saved)
    beam.symlink_to(other)
    with pytest.raises(BootstrapError):
        read_manifest(root)


@pytest.mark.asyncio
async def test_unsupported_hard_bound() -> None:
    with pytest.raises(NotImplementedError, match="hard graph memory bound"):
        async with stream(16, process, max_bytes=1024):
            pytest.fail("unsupported bound was accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, -1, 17, True])
async def test_count_rejected_before_native_import(count: int) -> None:
    with pytest.raises(ValueError, match="count must be"):
        async with stream(count, process):
            pytest.fail("unsupported count was accepted")


def test_core_does_not_load_genstage() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pybeam; assert 'pybeam.integrations.genstage' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
