import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx
import pytest

from tests.conftest import ROOT, RunningServer


@contextmanager
def serve_entry(
    request: pytest.FixtureRequest,
    directory: Path,
    entry: Path,
    port: int,
    *arguments: str,
) -> Generator[RunningServer]:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime")
    profile: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(profile, str)
    (directory / "pyproject.toml").write_text(
        "[tool.pybeam]\ndevelopment_runtime = " + json.dumps(str(Path(profile).resolve())) + "\n"
    )
    log_path = directory / "server.log"
    base_url = "http://127.0.0.1:{}".format(port)
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, str(entry), *arguments],
            cwd=directory,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), str(ROOT / "src")))),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(timeout=0.5) as client:
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()
                    try:
                        if client.get(base_url + "/health").status_code < 500:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError(log_path.read_text())
            yield RunningServer(base_url, port, log_path, process)
        finally:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
