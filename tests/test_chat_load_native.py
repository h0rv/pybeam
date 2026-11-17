import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from examples.chat_load.traffic import Plan, simulate

pytestmark = pytest.mark.runtime

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port: object = listener.getsockname()[1]
    if not isinstance(port, int):
        raise TypeError("Expected an integer TCP port")
    return port


def test_native__chat_rooms_share_counters_fanout_and_jobs(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and a built runtime")
    configured: object = request.config.getoption("hornbeam_runtime")
    if not isinstance(configured, str):
        raise TypeError("Runtime path must be text")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(Path(configured).resolve()) + '"\n'
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "src")])
    port = free_port()
    url = "http://127.0.0.1:" + str(port)
    log = tmp_path / "chat.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "examples.chat_load.app", "--port", str(port)],
            cwd=tmp_path,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            with httpx.Client(timeout=0.5) as client:
                while True:
                    assert process.poll() is None, log.read_text()
                    try:
                        if client.get(url + "/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.1)
            plan = Plan(url, rooms=2, clients=3, seconds=2, rate=5, jobs=5, job_size=1000)
            report = asyncio.run(simulate(plan, [process.pid]))
        finally:
            process.terminate()
            process.wait(timeout=15)
    assert report.connections == 6
    assert report.messages_acked == report.messages_sent > 0
    assert report.deliveries == report.deliveries_expected
    assert report.jobs_done + report.jobs_rejected == report.jobs_submitted
    assert report.counter_mismatches == 0
    assert report.errors == {}
