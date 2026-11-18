from pathlib import Path
from textwrap import indent

import pytest

from tests.native.task_helpers import run_native

pytestmark = pytest.mark.runtime


def test_soul__http_survives_inspection_stop_and_replacement(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if not request.config.getoption("run_runtime") or not request.config.getoption(
        "run_frameworks"
    ):
        pytest.skip("Requires --run-runtime --run-frameworks and the paired runtime")
    result = run_native(
        tmp_path,
        indent(
            """from examples.soul.main import View, live_view
import httpx
async with httpx.AsyncClient() as client:
    async with live_view(0) as demo:
        client.base_url = "http://" + demo.address
        assert (await client.get("/")).status_code == 200
        assert (await client.post("/run")).status_code == 202
        assert (await client.post("/run")).status_code == 409
        await demo.finished.wait()
        response = await client.get("/state")
        assert response.status_code == 200
        view = Json(View).decode(response.content)
        assert view.phase == "complete", view.error
        assert view.completed > 0 and view.failures == 0
        assert view.cpu_seconds > 0
        assert view.frame is not None
        assert view.frame.location.startswith("entries.py:")
        assert "in iterative_sum" in view.frame.location
        assert view.result == "500,000,000,500,000,000"
        assert len(view.samples) > 5
        assert all(point.milliseconds > 0 for point in view.samples)
        assert [event.label for event in view.events] == [
            "Started sum(-1)", "Found the non-yielding loop",
            "Stopped only the calculation", "Selected the corrected entry",
        ]
        edges = [0, *(event.seconds for event in view.events), float("inf")]
        for low, high in zip(edges, edges[1:]):
            assert any(low < point.seconds < high for point in view.samples)
        assert b"_erlang" not in response.content
        assert b"os_pid" not in response.content
        assert b"INFO REPORT" not in response.content
        assert b"/home/" not in response.content
        print("soul-view-ok")
        port = int(demo.address.rsplit(":", 1)[1])
    async with live_view(port) as replay:
        response = await client.get("/state")
        fresh = Json(View).decode(response.content)
        assert fresh.phase == "waiting"
        assert not fresh.samples and fresh.completed == 0
        assert not replay.finished.is_set()
        print("soul-immediate-restart-ok")
ready = asyncio.Event()
async def idle_server():
    async with live_view(0):
        ready.set()
        await asyncio.Future()
async with asyncio.TaskGroup() as group:
    idle = group.create_task(idle_server())
    await ready.wait()
    idle.cancel()
print("soul-cancelled-server-cleanup-ok")
""",
            "    ",
        ),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "soul-view-ok" in output
    assert "soul-immediate-restart-ok" in output
    assert "soul-cancelled-server-cleanup-ok" in output
    assert "Traceback (most recent call last)" not in output
