import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.native.runtime import ROOT, require_runtime


@pytest.mark.runtime
def test_queue__managed_ack_retry_cancellation_and_crash(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    require_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(ROOT / "_build/hornbeam") + '"\n'
    )
    entry = ROOT / "tests/fixtures/durable_queue.py"
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    result = subprocess.run(
        [sys.executable, str(entry), "proof"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "QUEUE_PROOF ack lease_retry release cancellation" in result.stdout
    log = tmp_path / "writer.log"
    with log.open("w") as output:
        writer = subprocess.Popen(
            [sys.executable, str(entry), "put"],
            cwd=tmp_path,
            env=environment,
            stdout=output,
            stderr=output,
        )
        try:
            marker = tmp_path / "stored.pid"
            deadline = time.monotonic() + 30
            while not marker.exists():
                assert writer.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.01)
            vm_pid = int(marker.read_text())
            assert vm_pid != writer.pid
            os.kill(vm_pid, signal.SIGKILL)
            writer.wait(timeout=20)
        finally:
            if writer.poll() is None:
                writer.terminate()
                writer.wait(timeout=20)
    recovered = subprocess.run(
        [sys.executable, str(entry), "recover"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "QUEUE_RECOVERED acknowledged_put" in recovered.stdout
