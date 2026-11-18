import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as async_connect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from pybeam.codec import Json
from tests.conftest import RunningServer, ServerFactory

pytestmark = pytest.mark.runtime


@dataclass(frozen=True, slots=True, kw_only=True)
class CounterReply:
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class TickReply:
    ticks: int


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputReply:
    active: int
    completed: int
    disconnected: int
    sent: int


def read_output_state(client: httpx.Client, timeout: float = 1) -> OutputReply:
    response = client.get("/output-state", timeout=timeout)
    response.raise_for_status()
    return Json(OutputReply).decode(response.content)


def test_native__nonreading_output_peer_closes_and_releases_subscription(
    service_server: RunningServer,
) -> None:
    with httpx.Client(base_url=service_server.base_url, timeout=1) as http:
        before = read_output_state(http)
        assert before.active == 0
        response = http.get("/subscriptions")
        response.raise_for_status()
        baseline = Json(CounterReply).decode(response.content).count
        response = http.get("/ticks")
        response.raise_for_status()
        ticks_before = Json(TickReply).decode(response.content).ticks
        url = "ws://127.0.0.1:{}/slow-output".format(service_server.port)
        with connect(
            url,
            open_timeout=5,
            max_queue=1,
            compression=None,
            max_size=16 * 1024 * 1024,
            close_timeout=1,
        ) as peer:
            deadline = time.monotonic() + 15
            while True:
                remaining = deadline - time.monotonic()
                assert remaining > 0, "Nonreading output handler did not finish within 15 seconds"
                state = read_output_state(http, timeout=min(1, remaining))
                if state.completed == before.completed + 1 and state.active == 0:
                    break
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            assert state.disconnected == before.disconnected + 1
            assert state.sent > before.sent
            response = http.get("/subscriptions")
            response.raise_for_status()
            assert Json(CounterReply).decode(response.content).count == baseline
            response = http.get("/ticks")
            response.raise_for_status()
            assert Json(TickReply).decode(response.content).ticks > ticks_before

            deadline = time.monotonic() + 5
            with pytest.raises(ConnectionClosed):
                # The nonreading peer may not observe the abortive close until it writes;
                # the closed server socket then resets it. An open one would pong.
                peer.ping()
                while True:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, (
                        "Peer transport remained open after output handler finished"
                    )
                    peer.recv(timeout=remaining)


def test_native__lifespan_worker_continues_after_startup(service_server: RunningServer) -> None:
    first = httpx.get(service_server.base_url + "/ticks")
    before = Json(TickReply).decode(first.content).ticks
    time.sleep(0.1)
    second = httpx.get(service_server.base_url + "/ticks")
    assert Json(TickReply).decode(second.content).ticks > before


def test_native__counter_updates_are_atomic(service_server: RunningServer) -> None:
    before = (
        Json(CounterReply).decode(httpx.get(service_server.base_url + "/counter").content).count
    )

    def increment(_index: int) -> int:
        response = httpx.post(service_server.base_url + "/counter", timeout=10)
        response.raise_for_status()
        return Json(CounterReply).decode(response.content).count

    updates = 32
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(increment, range(updates)))
    assert sorted(results) == list(range(before + 1, before + updates + 1))


@pytest.mark.parametrize("payload", [b"", b"caf\xc3\xa9", "雪".encode(), b"\x00\xff\x80"])
def test_native__byte_store_roundtrips(service_server: RunningServer, payload: bytes) -> None:
    namespace = uuid4().hex
    url = service_server.base_url + "/cache/" + namespace + "/value"
    response = httpx.put(url, content=payload)
    assert response.status_code == 204
    assert httpx.get(url).content == payload
    other = service_server.base_url + "/cache/" + uuid4().hex + "/value"
    assert httpx.get(other).status_code == 404


def test_native__idle_websocket_push_needs_no_client_poll(service_server: RunningServer) -> None:
    url = "ws://127.0.0.1:{}/idle".format(service_server.port)
    with connect(url, open_timeout=5) as client:
        assert client.recv(timeout=3) == "idle-push"


def test_native__five_item_messages_reach_asgi(service_server: RunningServer) -> None:
    url = "ws://127.0.0.1:{}/echo".format(service_server.port)
    message = json.dumps(["1", "1", "lv:probe", "phx_join", {}])
    with connect(url, open_timeout=5) as client:
        client.send(message)
        assert client.recv(timeout=3) == message


async def subscription_count(server: RunningServer) -> int:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(server.base_url + "/subscriptions")
        response.raise_for_status()
        return Json(CounterReply).decode(response.content).count


async def wait_subscription_count(server: RunningServer, expected: int) -> None:
    async with asyncio.timeout(5):
        while await subscription_count(server) != expected:
            await asyncio.sleep(0.025)


async def receive_until(connection: ClientConnection, target: int) -> list[int]:
    counts: list[int] = []
    async with asyncio.timeout(10):
        while not counts or counts[-1] < target:
            reply = Json(CounterReply).decode(await connection.recv())
            counts.append(reply.count)
    assert counts == sorted(counts)
    return counts


async def test_native__two_websocket_clients_share_state_and_pubsub(
    service_server: RunningServer,
) -> None:
    await wait_subscription_count(service_server, 0)
    url = "ws://127.0.0.1:{}/shared".format(service_server.port)
    async with async_connect(url) as first, async_connect(url) as second:
        initial = Json(CounterReply).decode(await first.recv()).count
        assert Json(CounterReply).decode(await second.recv()).count == initial
        await wait_subscription_count(service_server, 2)
        updates_per_client = 20
        target = initial + updates_per_client * 2

        async def update(connection: ClientConnection) -> None:
            for _ in range(updates_per_client):
                await connection.send(json.dumps({"action": "increment", "amount": 1}))

        async with asyncio.TaskGroup() as group:
            first_seen = group.create_task(receive_until(first, target))
            second_seen = group.create_task(receive_until(second, target))
            group.create_task(update(first))
            group.create_task(update(second))
        assert first_seen.result()[-1] == target
        assert second_seen.result()[-1] == target
        await first.send(json.dumps({"action": "cancel"}))
        async with asyncio.timeout(5):
            await first.wait_closed()
        await wait_subscription_count(service_server, 1)
        await second.send(json.dumps({"action": "snapshot"}))
        assert Json(CounterReply).decode(await second.recv()).count == target
    await wait_subscription_count(service_server, 0)
    async with async_connect(url) as reconnected:
        assert Json(CounterReply).decode(await reconnected.recv()).count == target
    await wait_subscription_count(service_server, 0)


async def test_native__slow_subscriber_overflows_without_retaining_runtime_owner(
    service_server: RunningServer,
) -> None:
    await wait_subscription_count(service_server, 0)
    url = "ws://127.0.0.1:{}/shared?slow=1".format(service_server.port)
    async with async_connect(url) as slow:
        Json(CounterReply).decode(await slow.recv())
        await wait_subscription_count(service_server, 1)
        async with httpx.AsyncClient(timeout=10) as client:
            for _ in range(96):
                response = await client.post(service_server.base_url + "/shared-increment")
                response.raise_for_status()
        await wait_subscription_count(service_server, 0)
    await wait_subscription_count(service_server, 0)


async def test_native__reconnect_misses_publications_and_reads_fresh_state(
    service_server: RunningServer,
) -> None:
    await wait_subscription_count(service_server, 0)
    url = "ws://127.0.0.1:{}/shared".format(service_server.port)
    async with async_connect(url) as first:
        initial = Json(CounterReply).decode(await first.recv()).count
    await wait_subscription_count(service_server, 0)
    async with httpx.AsyncClient(base_url=service_server.base_url) as client:
        for _ in range(3):
            response = await client.post("/shared-increment")
            response.raise_for_status()
        response = await client.post("/publish/vertical:state", content=b"missed")
        response.raise_for_status()
        assert response.json() == {"targeted": 0}
    async with async_connect(url) as reconnected:
        assert Json(CounterReply).decode(await reconnected.recv()).count == initial + 3
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reconnected.recv(), timeout=0.2)
        await reconnected.send(json.dumps({"action": "snapshot"}))
        assert Json(CounterReply).decode(await reconnected.recv()).count == initial + 3
    await wait_subscription_count(service_server, 0)


def test_native__node_stop_loses_shared_store_and_counters(
    runtime_server_factory: ServerFactory,
) -> None:
    namespace = uuid4().hex
    path = "/cache/" + namespace + "/value"
    with (
        runtime_server_factory("tests.fixtures.service_app:app") as first,
        httpx.Client(base_url=first.base_url) as client,
    ):
        assert client.put(path, content=b"volatile").status_code == 204
        assert client.get(path).content == b"volatile"
        assert client.post("/counter").json() == {"count": 1}
    with (
        runtime_server_factory("tests.fixtures.service_app:app") as second,
        httpx.Client(base_url=second.base_url) as client,
    ):
        assert client.get(path).status_code == 404
        assert client.get("/counter").json() == {"count": 0}


async def test_native__reconnect_runs_auth_and_mounts_current_authorized_state(
    service_server: RunningServer,
) -> None:
    await wait_subscription_count(service_server, 0)
    url = "ws://127.0.0.1:{}/authorized-shared".format(service_server.port)
    async with httpx.AsyncClient(base_url=service_server.base_url) as client:
        before = (await client.get("/auth-attempts")).json()["count"]
        async with async_connect(url + "?token=valid") as first:
            initial = Json(CounterReply).decode(await first.recv()).count
        await wait_subscription_count(service_server, 0)
        for _ in range(3):
            (await client.post("/shared-increment")).raise_for_status()
        async with async_connect(url + "?token=revoked") as rejected:
            with pytest.raises(ConnectionClosed) as closed:
                await rejected.recv()
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 1008
        assert (await client.get("/auth-attempts")).json()["count"] == before + 2
        await wait_subscription_count(service_server, 0)
        async with async_connect(url + "?token=valid") as fresh:
            assert Json(CounterReply).decode(await fresh.recv()).count == initial + 3
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(fresh.recv(), timeout=0.2)
        assert (await client.get("/auth-attempts")).json()["count"] == before + 3
    await wait_subscription_count(service_server, 0)


def test_native__shared_services_survive_actor_restart(service_server: RunningServer) -> None:
    path = "/cache/" + uuid4().hex + "/value"
    with httpx.Client(base_url=service_server.base_url) as client:
        assert client.put(path, content=b"survives actor").status_code == 204
        count = client.post("/counter").json()
        response = client.post("/crash-actor")
        response.raise_for_status()
        assert response.json() == {"count": 5}
        assert client.get(path).content == b"survives actor"
        assert client.get("/counter").json() == count


async def test_native__reconnect_docs_example(
    runtime_server_factory: ServerFactory,
) -> None:
    with runtime_server_factory("tests.fixtures.reconnect_app:app") as server:
        url = "ws://127.0.0.1:{}/counter".format(server.port)
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            async with async_connect(url + "?token=demo-token") as first:
                assert Json(CounterReply).decode(await first.recv()).count == 0
                await wait_subscription_count(server, 1)
                (await client.post("/increment")).raise_for_status()
                assert Json(CounterReply).decode(await first.recv()).count == 1
            await wait_subscription_count(server, 0)
            (await client.post("/increment")).raise_for_status()
            async with async_connect(url + "?token=revoked") as rejected:
                with pytest.raises(ConnectionClosed) as closed:
                    await rejected.recv()
                assert closed.value.rcvd is not None
                assert closed.value.rcvd.code == 1008
            await wait_subscription_count(server, 0)
            async with async_connect(url + "?token=demo-token") as fresh:
                assert Json(CounterReply).decode(await fresh.recv()).count == 2
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(fresh.recv(), timeout=0.2)
                (await client.post("/increment")).raise_for_status()
                assert Json(CounterReply).decode(await fresh.recv()).count == 3
            await wait_subscription_count(server, 0)
