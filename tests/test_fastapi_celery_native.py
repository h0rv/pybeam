import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from examples.fastapi_celery.app import Accepted, Status
from tests.test_chat_load_native import free_port

pytestmark = pytest.mark.runtime
ROOT = Path(__file__).resolve().parents[1]


async def observe(url: str) -> None:
    async with httpx.AsyncClient(base_url=url, timeout=10) as client:
        response = await client.post("/tasks", json={"type": 1})
        assert response.status_code == 201, response.text
        accepted = Accepted.model_validate_json(response.content)
        path = "/tasks/" + accepted.task_id
        async with client.stream("GET", path + "/events") as stream:
            assert stream.status_code == 200
            lines = [line async for line in stream.aiter_lines()]
        assert any("SUCCESS" in line for line in lines), lines
        assert any("PENDING" in line for line in lines), lines
        assert "event: done" in lines
        status = Status.model_validate_json((await client.get(path)).content)
        assert status.task_status == "SUCCESS"
        assert status.task_result is True


@pytest.mark.parametrize("shutdown", ["idle", "stream", "task"])
def test_native_task_get_and_push(
    request: pytest.FixtureRequest, tmp_path: Path, shutdown: str
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
    log = tmp_path / "tasks.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "examples.fastapi_celery.app",
                "--port",
                str(port),
                "--task-unit",
                "0.2",
            ],
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
                        if client.get(url + "/").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(0.1)
            asyncio.run(observe(url))
            with httpx.Client(base_url=url, timeout=10) as client:
                if shutdown == "idle":
                    process.terminate()
                else:
                    task_id = "unknown"
                    if shutdown == "task":
                        response = client.post("/tasks", json={"type": 1})
                        task_id = Accepted.model_validate_json(response.content).task_id
                    async_path = "/tasks/" + task_id + "/events"
                    with client.stream("GET", async_path) as stream:
                        lines = stream.iter_lines()
                        first_event: list[str] = []
                        for line in lines:
                            if not line:
                                break
                            first_event.append(line)
                        assert any("PENDING" in line for line in first_event)
                        process.terminate()
                        list(lines)
                process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
    assert process.returncode == 143
    assert log.read_text() == ""
