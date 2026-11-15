import asyncio
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
import pytest
from pydantic import ValidationError

from examples.discord_demo import main as web
from examples.discord_demo.domain import Action, Demo, RoomHandle, View
from examples.discord_simulation import Delivery, Event, Membership, Room, Snapshot
from pybeam.actors import ActorGroup, ActorInfo
from pybeam.actors.classes import Container, Route, definition
from pybeam.integrations.hornbeam import runtime as managed
from pybeam.integrations.hornbeam.contracts import ByteStore
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


@pytest.fixture
async def demo(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Demo]:
    instance = Demo(ActorGroup(), FakeServices())
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
    rendered = web.panel(view(identity, '<img src=x onerror="alert(1)">\n雪'), identity, demo)
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


async def test_post__ack_cannot_replace_chat_and_csrf_rejects_without_call(
    demo: Demo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        headers={"origin": web.owner.origin, "hx-request": "true"},
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


def local_room() -> Room:
    spec = definition(Room)
    payload = spec.constructor.encode(
        (),
        {
            "room_id": "general",
            "max_members": 8,
            "max_events": 100,
            "history_limit": 32,
        },
    )
    transport = LocalActor(spec.factory(payload)())
    return spec.reference(Room, Route(transport, spec.methods, Room))


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
    assert "PyBeam cleanup failed" not in text
    assert "cleanup was not acknowledged" not in text
