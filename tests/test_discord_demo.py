import argparse
import asyncio
import base64
import hashlib
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Self, override

import httpx
import pytest
from pydantic import ValidationError

from examples.discord_demo import main as web
from examples.discord_demo.domain import ROOMS, Action, Demo, Node, RoomHandle, View
from examples.discord_simulation import Delivery, Event, Membership, Room, Snapshot
from pybeam.actors import ActorGroup, ActorInfo
from pybeam.actors.classes import Container, Route, definition
from pybeam.integrations.hornbeam import runtime as managed
from pybeam.integrations.hornbeam.contracts import ByteStore, PresenceSubscription
from pybeam.runtime.loader import Entry

ROOT = Path(__file__).resolve().parents[1]


class FakeSubscription:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        self.closed = False

    async def __aenter__(self) -> Self:
        self.log.append("subscribe")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        return await self.receive()

    async def receive(self) -> bytes:
        return await self.queue.get()

    async def aclose(self) -> None:
        self.closed = True
        self.log.append("unsubscribe")


class FakeServices:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.subscription = FakeSubscription(self.log)

    def for_scope(self, scope: Mapping[str, object]) -> Self:
        return self

    def store(self, namespace: str) -> ByteStore:
        raise NotImplementedError

    async def incr(self, key: str, delta: int = 1, *, namespace: str = "default") -> int:
        raise NotImplementedError

    async def publish(self, topic: str, payload: bytes) -> int:
        self.log.append("publish")
        return 1

    def subscribe(self, topic: str, *, max_bytes: int = 65536) -> FakeSubscription:
        assert max_bytes == 4096
        return self.subscription

    async def subscription_count(self) -> int:
        return int(not self.subscription.closed)

    def presence(
        self, topic: str, key: str, payload: bytes, *, max_bytes: int = 65536
    ) -> PresenceSubscription:
        raise NotImplementedError


class RenderingDemo(Demo):
    @override
    async def tree(self) -> tuple[Node, ...]:
        return tuple(
            Node(room_id, "<0." + str(index + 100) + ".0>", 7, 0, 0)
            for index, room_id in enumerate(ROOMS)
        )


@pytest.fixture
async def demo(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Demo]:
    instance = RenderingDemo(ActorGroup(), FakeServices())
    monkeypatch.setattr(web, "owner", web.Owner(demo=instance))
    async with asyncio.TaskGroup() as tasks:
        web.owner.tasks = tasks
        yield instance
        web.owner.tasks = None


def view(identity: str, text: str = "hello") -> View:
    event = Event(room_id="general", event_id="one", epoch=7, text=text, tags=[identity])
    return View(
        Snapshot(
            "general",
            7,
            (Delivery(identity, 1),),
            ("one",),
            (event,),
            (Membership(identity, 7, 1, True, 3),),
        ),
        ActorInfo("<0.123.0>", 7, 0, 0),
    )


def cookie(identity: str) -> str:
    return identity + "." + web.owner.signature(identity)


@pytest.mark.parametrize("value", ["", "a" * 97, "a" * 32 + "." + "雪" * 64, None])
def test_session__rejects_invalid_or_nonascii_signatures(value: str | None) -> None:
    assert web.Owner().identity(value) is None


def test_session__signature_is_bound_to_runtime_and_csrf_purpose() -> None:
    owner = web.Owner()
    identity = secrets.token_hex(16)
    signed = identity + "." + owner.signature(identity)
    assert owner.identity(signed) == identity
    assert web.Owner().identity(signed) is None
    assert owner.csrf(identity) != owner.signature(identity)


@pytest.mark.parametrize(
    "payload",
    [
        {"generation": -1},
        {"generation": "nan"},
        {"generation": "1.0"},
        {"generation": True},
        {"generation": "00"},
        {"action": "eval"},
        {"text": "x" * 257},
        {"csrf": "雪" * 64},
        {"pid": "<0.1.0>"},
        {"member_id": "another-session"},
        {"generation": "9223372036854775808"},
        {"revision": "9223372036854775808"},
        {"revision": "-1"},
        {"revision": True},
        {"action": "leave"},
    ],
)
def test_command__rejects_unsafe_form_boundary(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        web.Command.model_validate(
            {
                "action": "send",
                "generation": "7",
                "csrf": "a" * 64,
                **payload,
            }
        )


async def test_render__escapes_messages_and_never_puts_chat_in_ack(demo: Demo) -> None:
    identity = secrets.token_hex(16)
    rendered = await web.panel(view(identity, '<img src=x onerror="alert(1)">\n雪'), identity, demo)
    assert "<img" not in rendered
    assert "&lt;img" in rendered
    assert "<0.123.0>" not in rendered
    assert "&lt;0.123.0&gt;" in rendered
    assert "Leave unavailable" not in rendered
    assert 'name="revision" value="3"' in rendered
    assert 'data-revision="3" data-present="true"' in rendered
    assert "including departed members" in rendered
    ack = web.templates.get_template("ack.html").render(message="Confirmed send")
    assert "messages" not in ack
    assert "generation" not in ack
    page = web.templates.get_template("index.html").render(
        rooms=("general",),
        room_id="general",
        member=identity,
    )
    assert 'id="room-stream" hx-sse:connect="/rooms/general/events" hx-swap="innerHTML"' in page
    assert 'hx-target="#ack"' in page
    assert "outerHTML" not in page
    assert "every " not in page
    assert "<details open>" not in rendered
    assert "Join this room to send" not in rendered


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"host": "attacker.invalid"}, 403),
        ({"origin": "https://attacker.invalid"}, 403),
        ({"sec-fetch-site": "cross-site"}, 403),
        ({"origin": "http://127.0.0.1:8766", "content-length": "5000"}, 413),
        ({"origin": "http://127.0.0.1:8766", "content-length": "invalid"}, 411),
        ({"origin": "http://127.0.0.1:8766", "transfer-encoding": "chunked"}, 403),
        ({}, 403),
    ],
)
async def test_http__rejects_host_origin_and_body_before_native_access(
    headers: dict[str, str],
    status: int,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
    ) as client:
        response = await client.post("/rooms/general/actions", headers=headers, content=b"x")
    assert response.status_code == status


@pytest.mark.parametrize("embedded", [False, True])
async def test_post__ack_cannot_replace_chat_and_csrf_rejects_without_call(
    demo: Demo,
    monkeypatch: pytest.MonkeyPatch,
    embedded: bool,
) -> None:
    if embedded:
        web.owner.embed_origin = web.LocalEmbedOrigin.parse("http://127.0.0.1:38921")
    calls: list[Action] = []
    identity = secrets.token_hex(16)

    async def change(
        room_id: str,
        member: str,
        generation: int,
        action: Action,
        text: str,
        event_id: str,
        *,
        revision: int | None = None,
    ) -> None:
        calls.append(action)

    monkeypatch.setattr(demo, "change", change)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        cookies={"discord": cookie(identity)},
        headers={
            "origin": web.owner.origin,
            "hx-request": "true",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        },
    ) as client:
        data = {"action": "send", "generation": "7", "csrf": "0" * 64, "text": "hello"}
        assert (await client.post("/rooms/general/actions", data=data)).status_code == 403
        assert not calls
        data["csrf"] = web.owner.csrf(identity)
        response = await client.post("/rooms/general/actions", data=data)
        assert response.status_code == 200
        assert "Confirmed send" in response.text
        assert "id=" not in response.text
        assert "hello" not in response.text
        assert calls == ["send"]
        assert demo.inflight == 0
        data["action"] = "crash"
        assert (await client.post("/rooms/general/actions", data=data)).status_code == 403
        assert calls == ["send"]


@pytest.mark.parametrize("origin", ["null", "http://localhost:8766", "https://attacker.invalid"])
async def test_origin__rejected_before_effect_and_valid_plain_form_redirects(
    demo: Demo, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    identity = secrets.token_hex(16)
    calls: list[Action] = []

    async def change(
        room_id: str,
        member: str,
        generation: int,
        action: Action,
        text: str,
        event_id: str,
        *,
        revision: int | None = None,
    ) -> None:
        calls.append(action)

    async def snapshot(room_id: str) -> View:
        return view(identity)

    monkeypatch.setattr(demo, "change", change)
    monkeypatch.setattr(demo, "view", snapshot)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        cookies={"discord": cookie(identity)},
    ) as client:
        data = {
            "action": "send",
            "generation": "7",
            "csrf": web.owner.csrf(identity),
            "text": "hello",
        }
        denied = await client.post("/rooms/general/actions", data=data, headers={"origin": origin})
        assert denied.status_code == 403
        assert "No room change was made" in denied.text
        assert calls == []
        accepted = await client.post(
            "/rooms/general/actions", data=data, headers={"origin": web.owner.origin}
        )
        assert accepted.status_code == 303
        assert calls == ["send"]
        page = await client.get(accepted.headers["location"])
        assert page.status_code == 200
        assert page.headers["referrer-policy"] == "same-origin"
        assert 'id="messages"' in page.text
        assert 'name="csrf"' in page.text
        assert "The action was confirmed" in page.text


async def test_stream__subscribes_before_snapshot_and_releases_on_close(
    demo: Demo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = FakeServices()
    demo.services = services
    identity = secrets.token_hex(16)

    async def snapshot(room_id: str) -> View:
        services.log.append("snapshot")
        return view(identity)

    monkeypatch.setattr(demo, "view", snapshot)
    lease = web.stream_owner("general", demo, identity)
    subscription = await anext(lease)
    stream = web.events("general", demo, identity, subscription)
    try:
        first = await anext(stream)
        assert "hello" in str(first.raw_data)
        assert services.log == ["subscribe", "snapshot"]
        services.subscription.queue.put_nowait(b"invalidation, not HTML")
        second = await anext(stream)
        assert "invalidation, not HTML" not in str(second.raw_data)
        assert services.log == ["subscribe", "snapshot", "snapshot"]
    finally:
        await stream.aclose()
        await lease.aclose()
    assert demo.streams == 0
    assert services.subscription.closed
    assert services.log[-1] == "unsubscribe"


async def test_stream__cancelled_receive_unsubscribes_without_polling(
    demo: Demo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = FakeServices()
    demo.services = services
    identity = secrets.token_hex(16)

    async def snapshot(room_id: str) -> View:
        return view(identity)

    monkeypatch.setattr(demo, "view", snapshot)
    lease = web.stream_owner("general", demo, identity)
    subscription = await anext(lease)
    stream = web.events("general", demo, identity, subscription)
    await anext(stream)
    async with asyncio.TaskGroup() as tasks:
        waiting = tasks.create_task(anext(stream))
        await asyncio.sleep(0)
        waiting.cancel()
    await lease.aclose()
    assert waiting.cancelled()
    assert services.subscription.closed
    assert demo.streams == 0


class LocalActor:
    def __init__(self, container: Container) -> None:
        self.container = container
        self.child_id = "unit-only"

    async def call(self, payload: bytes, /) -> bytes:
        return await self.container.dispatch(payload)

    async def rejoin(self) -> Self:
        return self


def local_room(room_id: str = "general") -> Room:
    spec = definition(Room)
    payload = spec.constructor.encode(
        (),
        {
            "room_id": room_id,
            "max_members": 8,
            "max_events": 100,
            "history_limit": 32,
        },
    )
    transport = LocalActor(spec.factory(payload)())
    return spec.reference(Room, Route(transport, spec.methods, Room))


async def test_tree__returns_four_observed_children(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = Demo(ActorGroup(), FakeServices())
    references = tuple(local_room(room_id) for room_id in ROOMS)
    instance.rooms.update(
        (room_id, RoomHandle(reference))
        for room_id, reference in zip(ROOMS, references, strict=True)
    )
    observed: list[object] = []

    async def info(reference: object) -> ActorInfo:
        observed.append(reference)
        for index, expected in enumerate(references):
            if reference is expected:
                return ActorInfo("<0." + str(index + 100) + ".0>", 7, 0, 0)
        raise AssertionError("Unknown test reference")

    monkeypatch.setattr(instance.group, "info", info)
    nodes = await instance.tree()
    assert tuple(observed) == references
    assert nodes == tuple(
        Node(room_id, "<0." + str(index + 100) + ".0>", 7, 0, 0)
        for index, room_id in enumerate(ROOMS)
    )


async def test_domain__native_snapshot_authorizes_members_and_rejects_stale_controls(
    demo: Demo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = local_room()
    demo.rooms["general"] = RoomHandle(reference)
    identity = secrets.token_hex(16)
    demo.owner = identity

    async def info(_reference: object) -> ActorInfo:
        return ActorInfo("<unit-only>", 7, 0, 0)

    monkeypatch.setattr(demo.group, "info", info)
    await demo.change("general", identity, 7, "join", "", "join")
    await demo.change("general", identity, 7, "send", "hello", "event")
    snapshot = await reference.snapshot()
    assert snapshot.members == (Delivery(identity, 1),)
    assert snapshot.history[0].text == "hello"
    with pytest.raises(ValueError, match="generation changed"):
        await demo.change("general", identity, 6, "crash", "", "crash")
    with pytest.raises(ValueError, match="Join this room"):
        await demo.change("general", "foreign", 7, "send", "forged", "forged")
    with pytest.raises(ValueError, match="membership revision captured"):
        await demo.change("general", identity, 7, "leave", "", "leave")
    assert await reference.snapshot() == snapshot
    await demo.change("general", identity, 7, "leave", "", "leave", revision=0)
    departed = await reference.snapshot()
    assert departed.members == ()
    assert departed.ledger == (Membership(identity, 7, 1, False, 0),)
    assert departed.history == snapshot.history
    await demo.change("general", identity, 7, "join", "", "rejoin")
    fresh = await reference.snapshot()
    assert fresh.ledger == (Membership(identity, 7, 1, True, 1),)
    with pytest.raises(web.ActorCallError, match="revision"):
        await demo.change("general", identity, 7, "leave", "", "old-leave", revision=0)
    assert await reference.snapshot() == fresh
    await demo.change("general", identity, 7, "leave", "", "fresh-leave", revision=1)
    assert (await reference.snapshot()).members == ()


async def test_post__cancelled_wait_does_not_cancel_or_retry_admitted_effect(
    demo: Demo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[str] = []
    identity = secrets.token_hex(16)

    async def change(
        room_id: str,
        member: str,
        generation: int,
        action: Action,
        text: str,
        event_id: str,
        *,
        revision: int | None = None,
    ) -> None:
        admitted.set()
        await release.wait()
        calls.append(event_id)
        completed.set()

    monkeypatch.setattr(demo, "change", change)
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web.app),
            base_url=web.owner.origin,
            cookies={"discord": cookie(identity)},
            headers={"origin": web.owner.origin},
        ) as client,
        asyncio.TaskGroup() as tasks,
    ):
        request = tasks.create_task(
            client.post(
                "/rooms/general/actions",
                data={
                    "action": "send",
                    "generation": "7",
                    "csrf": web.owner.csrf(identity),
                    "text": "hello",
                },
            )
        )
        await admitted.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not calls
        release.set()
        await completed.wait()
    assert len(calls) == 1
    assert demo.inflight == 0


async def test_stream__admission_rejects_before_subscribing(demo: Demo) -> None:
    demo.streams = 32
    lease = web.stream_owner("general", demo, secrets.token_hex(16))
    with pytest.raises(web.HTTPException) as error:
        await anext(lease)
    assert error.value.status_code == 503
    assert demo.streams == 32
    demo.streams = 0


async def test_leave_http__binds_session_and_preserves_captured_revision(
    demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = local_room()
    demo.rooms["general"] = RoomHandle(reference)
    first, second = secrets.token_hex(16), secrets.token_hex(16)

    async def info(_reference: object) -> ActorInfo:
        return ActorInfo("<unit-only>", 7, 0, 0)

    monkeypatch.setattr(demo.group, "info", info)
    for identity in (first, second):
        await demo.change("general", identity, 7, "join", "", "join")
    await demo.change("general", first, 7, "send", "first", "first-event")
    before = await reference.snapshot()
    captured = {
        "action": "leave",
        "generation": "7",
        "revision": "0",
        "csrf": web.owner.csrf(second),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        cookies={"discord": cookie(second)},
        headers={"origin": web.owner.origin, "hx-request": "true"},
    ) as client:
        for forged in (
            {**captured, "member_id": first},
            {**captured, "csrf": web.owner.csrf(first)},
            {**captured, "generation": "6"},
        ):
            response = await client.post("/rooms/general/actions", data=forged)
            assert response.status_code in (403, 422) or "Not confirmed" in response.text
            assert await reference.snapshot() == before
        response = await client.post("/rooms/general/actions", data=captured)
        assert "Confirmed leave" in response.text
        assert "id=" not in response.text
        assert (await reference.snapshot()).members == (Delivery(first, 1),)
        await demo.change("general", first, 7, "send", "second", "second-event")
        assert (await reference.snapshot()).ledger == (
            Membership(first, 7, 2, True),
            Membership(second, 7, 1, False),
        )
        await demo.change("general", second, 7, "join", "", "rejoin")
        fresh = await reference.snapshot()
        response = await client.post("/rooms/general/actions", data=captured)
        assert "Not confirmed" in response.text and "revision" in response.text
        assert await reference.snapshot() == fresh
        assert demo.inflight == 0


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:38921",
        "https://127.0.0.1:38921",
        "http://127.0.0.2:38921",
        "http://0.0.0.0:38921",
        "http://[::1]:38921",
        "http://attacker.invalid:38921",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:-1",
        "http://127.0.0.1:abc",
        "http://127.0.0.1:038921",
        "http://user@127.0.0.1:38921",
        "http://user:pass@127.0.0.1:38921",
        "http://127.0.0.1:38921/",
        "http://127.0.0.1:38921/chat",
        "http://127.0.0.1:38921?",
        "http://127.0.0.1:38921?x=1",
        "http://127.0.0.1:38921#",
        "http://127.0.0.1:38921#x",
        "*",
        "null",
        " http://127.0.0.1:38921",
        "http://127.0.0.1:38921\n",
        "http://127.0.0.1.attacker.invalid:38921",
    ],
)
def test_embed_origin__should_reject_noncanonical_or_nonlocal_input(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        web.LocalEmbedOrigin.parse(value)


@pytest.mark.parametrize("port", [1, 38921, 65535])
def test_embed_origin__should_parse_explicit_loopback_port(port: int) -> None:
    url = "http://127.0.0.1:" + str(port)
    assert web.LocalEmbedOrigin.parse(url).url == url


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("site", ["same-site", "same-origin"])
async def test_iframe__should_require_opt_in_and_emit_exact_ancestor(
    demo: Demo, monkeypatch: pytest.MonkeyPatch, enabled: bool, site: str
) -> None:
    if enabled:
        web.owner.embed_origin = web.LocalEmbedOrigin.parse("http://127.0.0.1:38921")
    identity = secrets.token_hex(16)

    async def snapshot(room_id: str) -> View:
        return view(identity)

    monkeypatch.setattr(demo, "view", snapshot)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        headers={"sec-fetch-site": site, "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"},
        follow_redirects=True,
    ) as client:
        response = await client.get("/")
        assert response.status_code == (200 if enabled else 403)
        if enabled:
            assert 'id="compose"' in response.text
            assert client.cookies.get("discord") is not None
            assert "SameSite=strict" in response.headers["set-cookie"]
            assert (
                "frame-ancestors http://127.0.0.1:38921;"
                in response.headers["content-security-policy"]
            )
            assert response.headers["referrer-policy"] == "same-origin"
            assert "access-control-allow-origin" not in response.headers
        else:
            assert demo.owner is None
            assert not client.cookies
        top = await client.get(
            "/rooms/general", headers={"sec-fetch-site": "none", "sec-fetch-dest": "document"}
        )
        assert top.status_code == 200
        if not enabled:
            assert "frame-ancestors 'none'" in top.headers["content-security-policy"]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("path", ["/", "/rooms/general"])
async def test_direct_document__same_site_get_should_require_opt_in_without_joining(
    demo: Demo, monkeypatch: pytest.MonkeyPatch, enabled: bool, path: str
) -> None:
    if enabled:
        web.owner.embed_origin = web.LocalEmbedOrigin.parse("http://127.0.0.1:38921")
    reference = local_room()
    demo.rooms["general"] = RoomHandle(reference)
    identity = secrets.token_hex(16)
    demo.owner = identity
    before = await reference.snapshot()

    async def info(_reference: object) -> ActorInfo:
        return ActorInfo("<unit-only>", 7, 0, 0)

    monkeypatch.setattr(demo.group, "info", info)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        cookies={"discord": cookie(identity)},
        headers={
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
        },
        follow_redirects=True,
    ) as client:
        response = await client.get(path)
    assert response.status_code == (200 if enabled else 403)
    if enabled:
        assert 'id="compose"' in response.text
        assert 'data-joined="false"' in response.text
        assert response.url.path == "/rooms/general"
        assert len(response.history) == (1 if path == "/" else 0)
    assert await reference.snapshot() == before
    assert demo.owner == identity
    assert demo.inflight == demo.streams == 0


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "method,path,changes",
    [
        ("GET", "/", {"sec-fetch-site": "cross-site"}),
        ("GET", "/rooms/general", {"origin": "http://127.0.0.1:38921"}),
        ("GET", "/rooms/general", {"origin": "null"}),
        ("GET", "/rooms/general", {"host": "127.0.0.1:38921"}),
        ("GET", "/rooms/general/events", {}),
        ("GET", "/resources", {}),
        ("POST", "/rooms/general/actions", {"origin": "http://127.0.0.1:8766"}),
    ],
)
async def test_direct_document__unsafe_requests_should_reject_before_state_or_admission(
    demo: Demo, enabled: bool, method: str, path: str, changes: dict[str, str]
) -> None:
    if enabled:
        web.owner.embed_origin = web.LocalEmbedOrigin.parse("http://127.0.0.1:38921")
    identity = secrets.token_hex(16)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        cookies={"discord": cookie(identity)},
    ) as client:
        response = await client.request(
            method,
            path,
            headers={
                "sec-fetch-site": "same-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                **changes,
            },
            data={"action": "join", "generation": "7", "csrf": web.owner.csrf(identity)},
        )
    assert response.status_code == 403
    assert demo.owner is None
    assert not demo.rooms
    assert demo.inflight == demo.streams == 0
    assert not response.cookies


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "method,path,headers",
    [
        ("GET", "/rooms/general", {"sec-fetch-mode": "cors"}),
        ("GET", "/rooms/general", {"sec-fetch-dest": "object"}),
        ("GET", "/rooms/general", {"sec-fetch-dest": "empty"}),
        ("GET", "/rooms/general", {"sec-fetch-site": "cross-site"}),
        ("GET", "/rooms/general", {"sec-fetch-mode": "no-cors"}),
        ("GET", "/rooms/general", {"host": "localhost:8766"}),
        ("GET", "/rooms/general", {"host": "127.0.0.1:38921"}),
        ("GET", "/rooms/general", {"origin": "null"}),
        ("GET", "/rooms/general", {"origin": "http://127.0.0.1:38921"}),
        ("GET", "/rooms/general", {"origin": "http://attacker.invalid:38921"}),
        ("GET", "/rooms/general/events", {}),
        ("GET", "/resources", {}),
        ("HEAD", "/rooms/general", {}),
        ("POST", "/rooms/general/actions", {"origin": "http://127.0.0.1:8766"}),
        ("POST", "/rooms/general/actions", {"origin": "http://127.0.0.1:38921"}),
        (
            "POST",
            "/rooms/general/actions",
            {"sec-fetch-dest": "empty", "origin": "http://127.0.0.1:8766"},
        ),
    ],
)
async def test_embedding__invalid_metadata_should_reject_before_state_or_admission(
    demo: Demo, enabled: bool, method: str, path: str, headers: dict[str, str]
) -> None:
    if enabled:
        web.owner.embed_origin = web.LocalEmbedOrigin.parse("http://127.0.0.1:38921")
    identity = secrets.token_hex(16)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        cookies={"discord": cookie(identity)},
    ) as client:
        response = await client.request(
            method,
            path,
            headers={
                "sec-fetch-site": "same-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "iframe",
                **headers,
            },
            data={
                "action": "send",
                "generation": "7",
                "csrf": web.owner.csrf(identity),
                "text": "must not be sent",
            },
        )
    assert response.status_code == 403
    assert demo.owner is None
    assert not demo.rooms
    assert demo.inflight == demo.streams == 0
    assert not response.cookies


async def test_showcase_chat__should_render_live_frame_under_strict_csp() -> None:
    from examples.showcase.app import create_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://127.0.0.1:38921"
    ) as client:
        response = await client.get("/chat")
        assert response.status_code == 200
        assert "Live FastAPI chat" in response.text
        assert 'src="http://127.0.0.1:8766/rooms/general"' in response.text
        assert 'href="http://127.0.0.1:8766/rooms/general"' in response.text
        css = response.text.split("<style>")[1].split("</style>")[0]
        digest = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
        policy = response.headers["content-security-policy"]
        assert "'sha256-" + digest + "'" in policy
        assert "frame-src http://127.0.0.1:8766;" in policy
        assert "frame-ancestors 'none'" in policy
        assert "'unsafe-inline'" not in policy
        assert (await client.get("/chat", headers={"host": "attacker.invalid"})).status_code == 403
        assert (await client.get("/chat", headers={"origin": "null"})).status_code == 403


@pytest.mark.parametrize("enabled", [False, True])
async def test_iframe_form__should_join_only_with_explicit_embedding_and_valid_security(
    demo: Demo, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    if enabled:
        web.owner.embed_origin = web.LocalEmbedOrigin.parse("http://127.0.0.1:38921")
    reference = local_room()
    demo.rooms["general"] = RoomHandle(reference)
    identity = secrets.token_hex(16)

    async def info(_reference: object) -> ActorInfo:
        return ActorInfo("<unit-only>", 7, 0, 0)

    monkeypatch.setattr(demo.group, "info", info)
    before = await reference.snapshot()
    form = {"action": "join", "generation": "7", "csrf": web.owner.csrf(identity)}
    headers = {
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "iframe",
        "origin": web.owner.origin,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app),
        base_url=web.owner.origin,
        cookies={"discord": cookie(identity)},
    ) as client:
        for changes in (
            {"host": "attacker.invalid"},
            {"origin": "null"},
            {"origin": "http://127.0.0.1:38921"},
            {"sec-fetch-site": "same-site"},
            {"sec-fetch-site": "cross-site"},
            {"sec-fetch-mode": "cors"},
            {"content-length": "5000"},
            {"content-length": "invalid"},
            {"transfer-encoding": "chunked"},
        ):
            denied = await client.post(
                "/rooms/general/actions", data=form, headers={**headers, **changes}
            )
            assert denied.status_code in (403, 411, 413)
            assert await reference.snapshot() == before
            assert demo.inflight == demo.streams == 0
        without_origin = {key: value for key, value in headers.items() if key != "origin"}
        denied = await client.post("/rooms/general/actions", data=form, headers=without_origin)
        assert denied.status_code == 403
        denied = await client.post(
            "/rooms/general/actions", data={**form, "csrf": "0" * 64}, headers=headers
        )
        assert denied.status_code == 403
        assert await reference.snapshot() == before
        accepted = await client.post("/rooms/general/actions", data=form, headers=headers)
    assert accepted.status_code == (303 if enabled else 403)
    if enabled:
        assert (await reference.snapshot()).members == (Delivery(identity, 0),)
    else:
        assert await reference.snapshot() == before
    assert demo.inflight == demo.streams == 0


@pytest.mark.parametrize("previous", [None, "disabled"])
@pytest.mark.parametrize("fails", [False, True])
def test_managed_services__restores_launch_environment(
    monkeypatch: pytest.MonkeyPatch, previous: str | None, fails: bool
) -> None:
    variable = "PYBEAM_HORNBEAM_SERVICES"
    if previous is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, previous)

    async def main() -> None:
        pass

    def invoke(entry: Entry) -> None:
        assert entry is main
        assert os.environ[variable] == "1"
        if fails:
            raise RuntimeError("launch failed")

    monkeypatch.setattr(managed, "run_native", invoke)
    if fails:
        with pytest.raises(RuntimeError, match="launch failed"):
            managed.run(main)
    else:
        managed.run(main)
    assert os.environ.get(variable) == previous


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.runtime
async def test_native__services_are_opt_in_and_cleanup_is_acknowledged(
    enabled: bool,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and staged native assets")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/native/discord_services.py",
        *([] if enabled else ["--without-services"]),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        async with asyncio.timeout(25):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
    text = output.decode()
    (tmp_path / "native-demo-startup.log").write_text(text)
    assert process.returncode == 0, text
    assert ("managed services drained" if enabled else "services remain opt-in") in text
    assert "PyBEAM cleanup failed" not in text
    assert "cleanup was not acknowledged" not in text
