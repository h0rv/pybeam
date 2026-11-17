import asyncio
import time

import httpx
import pytest
from pydantic import HttpUrl, ValidationError
from websockets.asyncio.server import ServerConnection, serve

from examples.chat_load import crash_test
from examples.chat_load.crash_test import (
    STEPS,
    Client,
    Dashboard,
    Observation,
    Room,
    Settings,
    Status,
    app,
    dashboard,
)
from examples.chat_load.faults import FaultKind
from examples.chat_load.protocol import Ack, Fault, Send


def test_room__empty_window_pending_age_outcomes_and_history() -> None:
    client = Client("test", state="connected")
    room = Room("room-1", [client])
    room.sample(10)
    assert room.p99 is None
    assert room.lines == []
    assert room.status(10) == "healthy"
    client.pending[1] = 9
    assert room.pending == 1
    assert room.oldest_ms(10) == 1000
    room.samples.extend([(9, 1), (10, 100)])
    room.sample(10)
    assert room.p99 == 99.01
    assert room.sample_count == 2
    assert room.lines
    room.mark("raise", "contained", 10)
    assert room.status(11) == "contained"
    assert room.status(17) == "healthy"
    client.state = "disconnected"
    assert room.status(11) == "disconnected"
    client.state = "reconnecting"
    assert room.status(11) == "reconnecting"
    room.sample(13)
    assert room.p99 is None
    assert room.sample_count == 0
    for tick in range(500):
        room.sample(tick)
    assert len(room.history) == 40


@pytest.mark.parametrize(
    "values", [{"rooms": 0}, {"rate": 100}, {"usual_url": "file:///etc/hosts"}]
)
def test_settings__reject_invalid_load_or_targets(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


@pytest.mark.parametrize(
    ("kind", "code", "body", "expected"),
    [
        ("raise", 200, Fault(kind="raise", outcome="contained").model_dump_json(), "contained"),
        ("raise", 500, "Internal Server Error", "failed"),
        ("hog", 200, Fault(kind="hog", outcome="completed").model_dump_json(), "completed"),
        ("abort", 200, "{}", "unknown"),
    ],
)
async def test_inject__reports_responses(
    kind: FaultKind,
    code: int,
    body: str,
    expected: Status,
) -> None:
    live = Dashboard(Settings(rooms=1, clients=1))
    side = live.sides["usual"]

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rooms/room-1/faults/" + kind
        return httpx.Response(code, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await live.inject(side, kind, http)
    assert side.rooms[0].notice == expected
    assert "HTTP " + str(code) in side.result or expected == "unknown"


@pytest.mark.parametrize("timed_out", [True, False])
async def test_inject__distinguishes_unknown_timeout_from_lost_connection(timed_out: bool) -> None:
    live = Dashboard(Settings(rooms=1, clients=1))

    async def fail(request: httpx.Request) -> httpx.Response:
        if timed_out:
            raise httpx.ReadTimeout("slow", request=request)
        raise httpx.RemoteProtocolError("closed", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        await live.inject(live.sides["usual"], "abort", http)
    assert live.sides["usual"].rooms[0].notice == ("unknown" if timed_out else "disconnected")


async def test_routes__single_run_is_owned_by_group_and_rejects_cross_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crash_test, "BASELINE_SECONDS", 10)
    live = Dashboard(Settings(rooms=2, clients=1))
    calls: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text=Fault(kind="raise", outcome="contained").model_dump_json())

    app.dependency_overrides[dashboard] = lambda: live
    try:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
            asyncio.TaskGroup() as group,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            live.group, live.http = group, http
            page = await client.get("/")
            assert page.status_code == 200
            assert 'hx-sse:connect="/events"' in page.text
            assert "Usual stack" in page.text and "PyBeam" in page.text
            assert "Start crash test" in page.text
            assert "/faults/usual/" not in page.text
            assert (await client.post("/faults/pybeam/raise")).status_code == 404
            assert (
                await client.post("/run", headers={"origin": "http://other"})
            ).status_code == 403
            assert not calls
            result = await client.post("/run")
            assert result.status_code == 200
            assert "STEP 1 OF 7" in result.text
            assert "disabled" in result.text
            assert live.running
            assert (await client.post("/run", headers={"origin": "http://test"})).status_code == 409
            assert live.run_task is not None
            assert not live.run_task.done()
            live.run_task.cancel()
    finally:
        app.dependency_overrides.clear()


def test_observation__only_other_rooms_and_window_peaks_count() -> None:
    live = Dashboard(Settings(rooms=2, clients=1))
    side = live.sides["usual"]
    target, other = side.rooms
    client = other.clients[0]
    client.drops, client.lost = 4, 5
    observation = Observation([(client, client.drops, client.lost)])
    live.observations["usual"] = observation
    live.phase, live.until = "observe", 12
    target.samples.append((10, 9000))
    other.samples.append((10, 23))
    client.pending[0] = 9
    live.sample_once(10)
    assert observation.measurement.p99 == 23
    assert observation.measurement.pending_ms == 1000

    empty = Observation([])
    empty.update([], 10)
    assert empty.measurement.p99 is None
    assert empty.measurement.dropped == empty.measurement.lost == 0
    client.pending[1] = 8
    live.record_pending(side, target, 10)
    assert observation.measurement.pending_ms == 1000
    live.record_pending(side, other, 10)
    assert observation.measurement.pending_ms == 2000
    del client.pending[1]
    client.drops += 2
    client.lost += 3
    client.pending.clear()
    live.sample_once(11)
    assert observation.measurement.dropped == 1
    assert observation.measurement.lost == 3
    other.samples.append((13, 5000))
    live.sample_once(13)
    assert observation.measurement.p99 == 23
    assert observation.measurement.pending_ms == 2000


async def test_scenario__concurrent_faults_transitions_results_and_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crash_test, "BASELINE_SECONDS", 0.01)
    monkeypatch.setattr(crash_test, "OBSERVE_SECONDS", 0.04)
    monkeypatch.setattr(crash_test, "RECOVERY_SECONDS", 0.01)
    arrivals: dict[str, set[str]] = {step.kind: set() for step in STEPS}
    paired = {step.kind: asyncio.Event() for step in STEPS}
    transitions: list[tuple[str, int]] = []
    pause = Dashboard.pause

    async def record_pause(self: Dashboard) -> None:
        transitions.append((self.phase, self.step_number))
        await pause(self)

    monkeypatch.setattr(Dashboard, "pause", record_pause)

    async def respond(request: httpx.Request) -> httpx.Response:
        assert live.phase == "observe"
        assert live.current is not None
        kind = live.current.kind
        assert request.url.path.endswith("/" + kind)
        arrivals[kind].add(request.url.host)
        if len(arrivals[kind]) == 2:
            paired[kind].set()
        await paired[kind].wait()
        now = time.monotonic()
        for side in live.sides.values():
            side.rooms[0].samples.append((now, 9000))
            side.rooms[1].samples.append((now, 7))
        live.sample_once(now)
        return httpx.Response(500)

    settings = Settings(
        usual_url=HttpUrl("http://usual"), pybeam_url=HttpUrl("http://pybeam"), rooms=2, clients=1
    )
    live = Dashboard(settings)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
        asyncio.TaskGroup() as group,
    ):
        live.group, live.http = group, http
        live.start_run()
        assert live.phase == "baseline"
        assert live.step_number == 1
        assert live.run_task is not None
        async with asyncio.timeout(2):
            await live.run_task
        assert live.phase == "done"
        assert not live.running
        assert live.step_number == 7
        assert [result.step.kind for result in live.results] == ["raise", "hog", "abort"]
        assert transitions == [
            ("baseline", 1),
            ("observe", 2),
            ("recovery", 3),
            ("observe", 4),
            ("recovery", 5),
            ("observe", 6),
            ("recovery", 7),
        ]
        for result in live.results:
            assert all(item.p99 == 7 for item in result.measurements.values())
            assert all("HTTP 500" in outcome for outcome in result.outcomes.values())
        assert live.total("usual").p99 == 7
        live.start_run()
        assert not live.results
        assert live.run_task is not None
        live.run_task.cancel()


async def test_scenario__missing_runner_is_unavailable() -> None:
    live = Dashboard(Settings())
    with pytest.raises(crash_test.HTTPException) as error:
        live.start_run()
    assert error.value.status_code == 503


async def test_traffic__reconnects_counts_lost_and_records_acknowledgements() -> None:
    ready = asyncio.Event()
    sessions = 0

    async def echo(socket: ServerConnection) -> None:
        nonlocal sessions
        sessions += 1
        async for payload in socket:
            send = Send.model_validate_json(payload)
            if sessions == 1:
                await socket.close()
                return
            await socket.send(Ack(id=send.id, count=1).model_dump_json())
            if send.id > 0:
                ready.set()

    async with serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        live = Dashboard(
            Settings(usual_url=HttpUrl("http://127.0.0.1:" + str(port)), rooms=1, clients=1)
        )
        side = live.sides["usual"]
        room = side.rooms[0]
        client = room.clients[0]
        async with asyncio.TaskGroup() as tasks:
            task = tasks.create_task(live.run_client(side, room, client))
            try:
                async with asyncio.timeout(6):
                    await ready.wait()
                assert client.reconnects == 1
                assert client.lost == 1
                assert room.connected == 1
                assert room.samples
            finally:
                task.cancel()
        assert room.connected == 0
        assert not client.pending
