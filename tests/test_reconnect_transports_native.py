import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from pydantic import BaseModel, TypeAdapter
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from tests.conftest import ServerFactory
from tests.test_runtime_native import wait_subscription_count

pytestmark = pytest.mark.runtime


class RenderResponse(BaseModel):
    rendered: dict[str, object]


class JoinPayload(BaseModel):
    status: str
    response: RenderResponse


join_reply = TypeAdapter(tuple[str | None, str | None, str, str, JoinPayload])
diff_reply = TypeAdapter(tuple[str | None, str | None, str, str, dict[str, object]])


@dataclass(frozen=True)
class Join:
    topic: str
    csrf: str
    session: str
    url: str

    async def send(self, socket: ClientConnection) -> None:
        await socket.send(
            json.dumps(
                [
                    "1",
                    "1",
                    self.topic,
                    "phx_join",
                    {
                        "url": self.url,
                        "session": self.session,
                        "params": {"_csrf_token": self.csrf},
                    },
                ]
            )
        )


def extract(pattern: str, html: str) -> str:
    match = re.search(pattern, html)
    assert match is not None, pattern
    value = match.group(1)
    assert isinstance(value, str)
    return value


async def stats(client: httpx.AsyncClient) -> dict[str, int]:
    response = await client.get("/health")
    response.raise_for_status()
    return TypeAdapter(dict[str, int]).validate_json(response.content)


async def wait_pyview_subscriptions(client: httpx.AsyncClient, count: int) -> None:
    async with asyncio.timeout(5):
        while (await stats(client))["count"] != count:  # noqa: ASYNC110
            await asyncio.sleep(0.025)


async def test_native__pyview_reconnect_auth_mount_and_no_replay(
    runtime_server_factory: ServerFactory,
) -> None:
    with runtime_server_factory("tests.fixtures.pyview_reconnect_app:app") as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            (await client.get("/session?token=valid")).raise_for_status()
            page = await client.get("/")
            page.raise_for_status()
            join = Join(
                topic="lv:" + extract(r'id="(phx-[^"]+)"', page.text),
                csrf=extract(r'name="csrf-token" content="([^"]+)"', page.text),
                session=extract(r'data-phx-session="([^"]+)"', page.text),
                url=server.base_url + "/",
            )
            url = "ws://127.0.0.1:{}/live/websocket".format(server.port)
            async with connect(
                url, additional_headers={"Cookie": "session=" + client.cookies["session"]}
            ) as first:
                await join.send(first)
                reply = join_reply.validate_json(await first.recv())
                assert reply[4].status == "ok"
                assert reply[4].response.rendered["0"] == "0"
                assert reply[4].response.rendered["1"] == "0"
                assert await stats(client) == {"auth": 1, "mounts": 1, "count": 1}
                (await client.post("/increment")).raise_for_status()
                diff = diff_reply.validate_json(await first.recv())
                assert diff[3] == "diff"
                assert diff[4]["0"] == "1"
                assert diff[4]["1"] == "1"
            await wait_pyview_subscriptions(client, 0)
            gap = await client.post("/increment")
            assert gap.json() == {"count": 2, "targeted": 0}
            (await client.get("/session?token=revoked")).raise_for_status()
            async with connect(
                url, additional_headers={"Cookie": "session=" + client.cookies["session"]}
            ) as rejected:
                await join.send(rejected)
                with pytest.raises(ConnectionClosed) as closed:
                    await rejected.recv()
                assert closed.value.rcvd is not None
                assert closed.value.rcvd.code == 1000
            assert await stats(client) == {"auth": 2, "mounts": 1, "count": 0}
            (await client.get("/session?token=valid")).raise_for_status()
            async with connect(
                url, additional_headers={"Cookie": "session=" + client.cookies["session"]}
            ) as fresh:
                await join.send(fresh)
                reply = join_reply.validate_json(await fresh.recv())
                assert reply[4].status == "ok"
                assert reply[4].response.rendered["0"] == "2"
                assert reply[4].response.rendered["1"] == "0"
                assert await stats(client) == {"auth": 3, "mounts": 2, "count": 1}
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(fresh.recv(), timeout=0.2)
                (await client.post("/increment")).raise_for_status()
                diff = diff_reply.validate_json(await fresh.recv())
                assert diff[4]["0"] == "3"
                assert diff[4]["1"] == "1"
            await wait_pyview_subscriptions(client, 0)


async def next_event(lines: AsyncIterator[str]) -> list[str]:
    event: list[str] = []
    async with asyncio.timeout(5):
        async for line in lines:
            if line:
                event.append(line)
            elif event:
                return event
    raise AssertionError("Event stream ended")


async def auth_attempts(client: httpx.AsyncClient) -> int:
    response = await client.get("/auth-attempts")
    response.raise_for_status()
    return TypeAdapter(dict[str, int]).validate_json(response.content)["count"]


async def test_native__sse_reconnect_auth_snapshot_and_last_event_id(
    runtime_server_factory: ServerFactory,
) -> None:
    with runtime_server_factory("tests.fixtures.reconnect_app:app") as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            async with client.stream("GET", "/events?token=valid") as first:
                assert first.status_code == 200
                lines = first.aiter_lines()
                assert await next_event(lines) == ["data: 0", "id: 0"]
                assert await auth_attempts(client) == 1
                (await client.post("/increment")).raise_for_status()
                assert await next_event(lines) == ["data: 1", "id: 1"]
                assert await anext(lines, None) is None
            await wait_subscription_count(server, 0)
            (await client.post("/increment")).raise_for_status()
            async with client.stream("GET", "/events?token=revoked") as rejected:
                assert rejected.status_code == 403
                assert "text/event-stream" not in rejected.headers["content-type"]
            assert await auth_attempts(client) == 2
            async with client.stream(
                "GET", "/events?token=valid", headers={"Last-Event-ID": "0"}
            ) as fresh:
                assert fresh.status_code == 200
                lines = fresh.aiter_lines()
                assert await next_event(lines) == ["data: 2", "id: 2"]
                assert await auth_attempts(client) == 3
                (await client.post("/increment")).raise_for_status()
                assert await next_event(lines) == ["data: 3", "id: 3"]
                assert await anext(lines, None) is None
            await wait_subscription_count(server, 0)
