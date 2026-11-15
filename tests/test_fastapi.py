import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing, asynccontextmanager
from typing import Annotated

import pytest
from anyio import BrokenResourceError
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from hornbeam.api.errors import RuntimeUnavailable
from httpx import ASGITransport, AsyncClient
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from examples.fastapi.main import app as example_app
from examples.fastapi.main import counter_events, counter_subscription
from examples.fastapi.main import provider as example_provider
from pybeam.integrations.fastapi.dependencies import BeamProvider
from pybeam.integrations.hornbeam import Services
from tests.fakes import FakeServices, ServiceState


def fake_app(state: ServiceState) -> FastAPI:
    async def connect() -> Services:
        return FakeServices(state, owner="application-owner")

    provider = BeamProvider(connect)
    app = FastAPI(lifespan=provider.lifespan)
    app.include_router(APIRouter(routes=example_app.routes))
    app.dependency_overrides[example_provider] = provider
    app.dependency_overrides[counter_subscription] = provider.subscription("demo:counter")
    return app


def with_test_owner(app: ASGIApp) -> ASGIApp:
    async def owned_app(scope: Scope, receive: Receive, send: Send) -> None:
        scope.setdefault("extensions", {})["hornbeam.owner"] = object()
        await app(scope, receive, send)

    return owned_app


@asynccontextmanager
async def fake_client(state: ServiceState) -> AsyncGenerator[AsyncClient]:
    app = fake_app(state)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(with_test_owner(app)), base_url="http://testserver"
        ) as client,
    ):
        yield client


async def test_fastapi__normal_and_enhanced_forms() -> None:
    state = ServiceState()
    async with fake_client(state) as client:
        assert (await client.get("/health")).text == "ok"
        page = await client.get("/")
        assert page.status_code == 200
        assert 'hx-sse:connect="http://testserver/events"' in page.text
        assert "hx-ext" not in page.text
        assert "sse-swap" not in page.text
        assert 'hx-target="#confirmation"' in page.text
        assert "/static/htmx.min.js" in page.text
        assert "/static/sse.js" in page.text
        assert "<output" in (await client.get("/counter")).text
        response = await client.post("/increment", data={"amount": "10"}, follow_redirects=False)
        assert response.status_code == 303
        response = await client.post(
            "/increment",
            data={"amount": "1"},
            headers={"hx-request": "true", "origin": "http://testserver"},
        )
        assert response.status_code == 200
        assert response.text == "Added 1."
        assert "output" not in response.text
        assert (await client.get("/counter")).text == '<output id="count">11</output>'
    assert ("publish", ["demo:counter", b"11"]) in state.calls


@pytest.mark.parametrize(
    "body",
    [
        "",
        "amount=",
        "amount=0",
        "amount=11",
        "amount=-1",
        "amount=1.5",
        "amount=true",
        "other=1",
        "amount=%3Cscript%3E",
    ],
)
async def test_fastapi__rejects_invalid_form(body: str) -> None:
    state = ServiceState()
    async with fake_client(state) as client:
        response = await client.post(
            "/increment",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 422
    assert not state.calls


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"origin": "http://testserver.evil.example"},
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
        {"referer": "https://evil.example/form"},
        {"referer": "http://["},
    ],
)
async def test_fastapi__denies_cross_origin_mutations(headers: dict[str, str]) -> None:
    state = ServiceState()
    async with fake_client(state) as client:
        response = await client.post("/increment", data={"amount": "1"}, headers=headers)
    assert response.status_code == 403
    assert not state.calls


async def test_fastapi__declares_form_validation() -> None:
    state = ServiceState()
    async with fake_client(state) as client:
        assert (await client.post("/increment", json={"amount": 1})).status_code == 422
        schema = await client.get("/openapi.json")
    assert b"application/x-www-form-urlencoded" in schema.content
    assert b'"minimum":1' in schema.content
    assert b'"maximum":10' in schema.content
    assert not state.calls


async def test_fastapi__provider_requires_lifespan_and_fails_without_runtime() -> None:
    provider = BeamProvider()
    with pytest.raises(RuntimeError, match="lifespan"):
        await provider(HTTPConnection(event_scope()))
    with pytest.raises(RuntimeUnavailable, match="Hornbeam"):
        async with provider.lifespan(FastAPI()):
            pytest.fail("A native connection must not become a local fallback")


async def test_fastapi__snapshot_subscribes_first_and_cleans_up_on_close() -> None:
    state = ServiceState()
    state.update_during_snapshot = True
    beam = FakeServices(state, owner="app").for_scope({"extensions": {"hornbeam.owner": "request"}})
    async with (
        beam.subscribe("demo:counter") as subscription,
        aclosing(counter_events(beam, subscription)) as stream,
    ):
        assert (await anext(stream)).raw_data == '<output id="count">0</output>'
        assert state.calls[0][0] == "subscribe"
        assert state.calls[0][1][0] == "request"
        assert (await anext(stream)).raw_data == '<output id="count">1</output>'
    assert not state.channels
    assert state.calls[-1][0] == "unsubscribe"


async def test_fastapi__cancelled_receive_releases_subscription() -> None:
    state = ServiceState()
    beam = FakeServices(state, owner="request")
    async with (
        beam.subscribe("demo:counter") as subscription,
        aclosing(counter_events(beam, subscription)) as stream,
    ):
        await anext(stream)
        channel = next(iter(state.channels.values()))
        async with asyncio.TaskGroup() as group:
            task = group.create_task(anext(stream))
            await channel.waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert not state.channels


async def test_fastapi__missing_scope_owner_is_an_error() -> None:
    state = ServiceState()
    app = fake_app(state)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client,
    ):
        with pytest.raises(RuntimeError, match="Hornbeam"):
            await client.get("/counter")


async def test_fastapi__connects_once_and_keeps_scope_owners_independent() -> None:
    state = ServiceState()
    connections = 0
    lifecycle: list[str] = []

    async def connect() -> Services:
        nonlocal connections
        connections += 1
        return FakeServices(state, owner="application-owner")

    provider = BeamProvider(connect)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        lifecycle.append("starting")
        async with provider.lifespan(app):
            lifecycle.append("ready")
            try:
                yield
            finally:
                lifecycle.append("stopping")
        lifecycle.append("stopped")

    app = FastAPI(lifespan=lifespan)
    first = HTTPConnection(event_scope())
    second_scope = event_scope()
    second_scope["extensions"] = {"hornbeam.owner": "second-owner"}
    second = HTTPConnection(second_scope)
    dependency = provider.subscription("demo:counter", max_bytes=1024)
    async with app.router.lifespan_context(app):
        assert lifecycle == ["starting", "ready"]
        with pytest.raises(RuntimeError, match="already running"):
            async with provider.lifespan(app):
                pytest.fail("Nested provider lifespan must be rejected")
        async with aclosing(dependency(first)) as first_stream:
            first_subscription = await anext(first_stream)
            async with aclosing(dependency(second)) as second_stream:
                second_subscription = await anext(second_stream)
                assert first_subscription is not second_subscription
                assert len(state.channels) == 2
                owners = [args[0] for action, args in state.calls if action == "subscribe"]
                assert owners == ["request-owner", "second-owner"]
                assert all(channel.max_bytes == 1024 for channel in state.channels.values())
            assert len(state.channels) == 1
            await (await provider(first)).publish("demo:counter", b"still-open")
            assert await first_subscription.receive() == b"still-open"
        assert not state.channels
        assert connections == 1
    assert lifecycle == ["starting", "ready", "stopping", "stopped"]
    with pytest.raises(RuntimeError, match="lifespan"):
        await provider(first)


async def test_fastapi__websocket_dependency_uses_actual_connection_scope() -> None:
    state = ServiceState()
    connections = 0

    async def connect() -> Services:
        nonlocal connections
        connections += 1
        return FakeServices(state, owner="application-owner")

    provider = BeamProvider(connect)
    app = FastAPI(lifespan=provider.lifespan)
    scope: Scope = {
        "type": "websocket",
        "path": "/socket",
        "query_string": b"",
        "headers": [],
        "extensions": {"hornbeam.owner": "websocket-owner"},
    }
    sent: list[Message] = []

    @app.websocket("/socket")
    async def socket_endpoint(
        socket: WebSocket,
        connection: HTTPConnection,
        beam: Annotated[Services, Depends(provider)],
    ) -> None:
        assert connection is socket
        assert connection.scope is scope
        await socket.accept()
        async with beam.subscribe("demo:counter"):
            await socket.send_text("subscribed")
        await socket.close()

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    async with app.router.lifespan_context(app):
        await app(scope, receive, send)
    assert connections == 1
    assert [message["type"] for message in sent] == [
        "websocket.accept",
        "websocket.send",
        "websocket.close",
    ]
    assert state.calls[0][1][0] == "websocket-owner"
    assert not state.channels


def event_scope(path: str = "/events") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "extensions": {"hornbeam.owner": "request-owner"},
    }


async def test_fastapi__real_asgi_sse_framing_and_disconnect_cleanup() -> None:
    state = ServiceState()
    app = fake_app(state)
    disconnected = asyncio.Event()
    frames: list[bytes] = []

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            assert message["status"] == 200
            assert (b"content-type", b"text/event-stream; charset=utf-8") in message["headers"]
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            assert isinstance(body, bytes)
            if body:
                frames.append(body)
                if len(frames) == 1:
                    beam = FakeServices(state, owner="publisher")
                    count = await beam.incr("shared", 1, namespace="demo")
                    await beam.publish("demo:counter", b"<script>alert(1)</script>")
                    assert count == 1
                else:
                    disconnected.set()

    async with app.router.lifespan_context(app), asyncio.timeout(5):
        await app(event_scope(), receive, send)
    assert frames == [
        b'data: <output id="count">0</output>\n\n',
        b'data: <output id="count">1</output>\n\n',
    ]
    assert state.calls[0][1][0] == "request-owner"
    assert not state.channels


async def test_fastapi__mounted_urls_and_redirect() -> None:
    state = ServiceState()
    child = fake_app(state)
    parent = FastAPI()
    parent.mount("/demo", child)
    async with (
        child.router.lifespan_context(child),
        AsyncClient(
            transport=ASGITransport(with_test_owner(parent)), base_url="http://testserver"
        ) as client,
    ):
        page = await client.get("/demo/")
        assert page.status_code == 200
        for attribute, path in (
            ("src", "/static/htmx.min.js"),
            ("src", "/static/sse.js"),
            ("hx-sse:connect", "/events"),
            ("action", "/increment"),
            ("hx-post", "/increment"),
            ("href", "/counter"),
        ):
            assert attribute + '="http://testserver/demo' + path + '"' in page.text
        for path in ("/demo/static/htmx.min.js", "/demo/static/sse.js"):
            assert (await client.get(path)).status_code == 200
        response = await client.post(
            "/demo/increment",
            data={"amount": "1"},
            headers={"referer": "http://testserver/demo/"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "http://testserver/demo/"


async def test_fastapi__backpressured_sse_releases_request_subscription() -> None:
    state = ServiceState()
    child = fake_app(state)
    parent = FastAPI()
    parent.mount("/demo", child)
    disconnected = asyncio.Event()
    first_body = asyncio.Event()
    never_unblock = asyncio.Event()

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()
            await never_unblock.wait()

    async def run_request() -> None:
        try:
            await parent(event_scope("/demo/events"), receive, send)
        except* BrokenResourceError:
            assert not state.channels

    async with (
        child.router.lifespan_context(child),
        asyncio.timeout(5),
        asyncio.TaskGroup() as group,
    ):
        task = group.create_task(run_request())
        await first_body.wait()
        channel = next(iter(state.channels.values()))
        publisher = FakeServices(state, owner="publisher")
        for _ in range(10):
            await publisher.incr("shared", 1, namespace="demo")
            await publisher.publish("demo:counter", b"advisory")
            await asyncio.sleep(0)
        assert not channel.queue.empty()
        disconnected.set()
        await task
        assert not state.channels
        assert state.calls[-1][0] == "unsubscribe"
        assert state.calls[0][1][0] == "request-owner"
        assert not never_unblock.is_set()
