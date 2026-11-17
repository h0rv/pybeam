import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from examples.showcase.catalog import ROOT
from pybeam.codec import Json
from pybeam.runtime.ownership import identity, resolve
from tests.fixtures.showcase_shutdown import CloseEvidence, JobEvidence


def wait_for_text(path: Path, text: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 8
    while not path.is_file() or text not in path.read_text():
        if process.poll() is not None or time.monotonic() >= deadline:
            raise AssertionError("Startup or owned Python child did not become ready: " + str(path))
        time.sleep(0.02)


@pytest.mark.parametrize("owned_python_job", [False, True], ids=["idle", "owned-python-hold"])
def test_sigterm__held_production_sse_reaches_lifespan_and_owned_cleanup(
    tmp_path: Path,
    owned_python_job: bool,
) -> None:
    # Fixed private ports per case, never retries. The production CLI supplies Uvicorn settings.
    port = 38943 if owned_python_job else 38942
    base = "http://127.0.0.1:" + str(port)
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    log_path = tmp_path / "uvicorn.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.fixtures.showcase_shutdown",
                str(tmp_path),
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        owner = identity(process.pid)
        try:
            wait_for_text(log_path, "Uvicorn running on", process)
            with (
                httpx.Client(base_url=base, timeout=8) as client,
                client.stream("GET", "/events") as stream,
            ):
                assert stream.status_code == 200
                lines = stream.iter_lines()
                assert next(lines) == "event: jobs-changed"
                # The client keeps this connection open until AFTER the server exits.
                if owned_python_job:
                    response = client.post("/start/hold", headers={"origin": base})
                    assert response.status_code == 303
                    wait_for_text(tmp_path / "child-ready.txt", "pid", process)
                start = time.monotonic()
                process.send_signal(signal.SIGTERM)
                returncode = process.wait(timeout=6)
                elapsed = time.monotonic() - start
                assert returncode in (0, -signal.SIGTERM)
                assert elapsed < 6
                assert not stream.is_closed
                close = Json(CloseEvidence).decode((tmp_path / "close.json").read_text())
                assert close.closing
                assert (tmp_path / "subscribers-after-disconnect.txt").read_text() == "0"
                assert resolve(owner) is None
                if owned_python_job:
                    assert close.stop_requests == (True,)
                    job = Json(JobEvidence).decode((tmp_path / "job-finished.json").read_text())
                    assert job.status == "stopped"
                    assert job.cleanup_acknowledged
                    assert job.returncode == 0
                    assert resolve(job.birth) is None
                    print(
                        "Controlled Python birth gone: "
                        + Json(type(job.birth)).encode(job.birth).decode()
                    )
                else:
                    assert close.stop_requests == ()
                print(
                    "Held SSE SIGTERM {}: {:.3f}s, close reached, subscribers=0".format(
                        "owned Python" if owned_python_job else "idle",
                        elapsed,
                    )
                )
        finally:
            # A failing baseline may finish only after the stream context disconnects.
            # Never use SIGKILL or count that fallback as a successful shutdown.
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)
