import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import HttpUrl, ValidationError
from websockets.asyncio.server import ServerConnection, serve

from examples.chat_load.crash_test import (
    Client,
    Dashboard,
    Room,
    Settings,
    Status,
    app,
    dashboard,
    http_client,
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


async def test_routes__render_validate_and_reject_cross_origin_faults() -> None:
    live = Dashboard(Settings(rooms=1, clients=1))
    calls: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text=Fault(kind="raise", outcome="contained").model_dump_json())

    async def http() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            yield client

    app.dependency_overrides[dashboard] = lambda: live
    app.dependency_overrides[http_client] = http
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            page = await client.get("/")
            assert page.status_code == 200
            assert 'hx-sse:connect="/events"' in page.text
            assert "Usual stack" in page.text and "PyBeam" in page.text
            assert (await client.post("/faults/unknown/raise")).status_code == 422
            assert (await client.post("/faults/pybeam/unknown")).status_code == 422
            assert (
                await client.post("/faults/pybeam/raise", headers={"origin": "http://other"})
            ).status_code == 403
            assert not calls
            result = await client.post("/faults/pybeam/raise")
            assert result.status_code == 200
            assert "contained" in result.text
            assert calls == ["/rooms/room-1/faults/raise"]
            assert not live.sides["pybeam"].running
    finally:
        app.dependency_overrides.clear()


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
