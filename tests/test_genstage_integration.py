import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from examples.genstage_callbacks import process
from pybeam.integrations.genstage import enable, stream

ROOT = Path(__file__).resolve().parents[1]


def test_missing_optional_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYBEAM_GENSTAGE_ASSETS", str(tmp_path))
    with pytest.raises(RuntimeError, match="Optional GenStage runtime artifact is unavailable"):
        enable()


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


def test_owned_managed_shutdown() -> None:
    assets = ROOT / ".experiment/genstage-assets"
    if not assets.is_dir():
        pytest.skip("privately staged optional GenStage assets are not available")
    environment = dict(os.environ)
    environment["PYBEAM_GENSTAGE_ASSETS"] = str(assets)
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    with subprocess.Popen(
        [sys.executable, str(ROOT / "tests/native/genstage_shutdown.py")],
        cwd=ROOT,
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
