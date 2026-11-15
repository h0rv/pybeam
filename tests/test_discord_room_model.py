import asyncio
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from examples.discord_simulation import Delivery, Event, Membership, Receipt, Room, Snapshot
from pybeam.actors.classes import Container, Definition, definition
from pybeam.actors.codec import ActorCallError, ActorPayloadError, decode_reply

type Action = Literal[
    "join-a",
    "join-b",
    "leave-a",
    "leave-b",
    "stale-a",
    "stale-b",
    "send-0",
    "send-1",
    "wrong-room",
    "reset",
]


async def invoke(
    spec: Definition, container: Container, name: str, *args: object, **kwargs: object
) -> object:
    index, method = next(
        (i, method) for i, method in enumerate(spec.methods) if method.name == name
    )
    payload = index.to_bytes(4) + method.arguments.encode(args, kwargs)
    return method.decode(decode_reply(await container.dispatch(payload)))


async def check_trace(actions: list[Action]) -> None:
    # Rejected/stale leaves never change presence or receipts. Departures remove
    # only future fanout, not past deliveries. Reset is the only ledger erasure.
    # This exercises JSON/domain transitions, not native process ownership.
    spec = definition(Room)
    constructor = spec.constructor.encode(
        (), {"room_id": "room", "max_members": 2, "max_events": 2, "history_limit": 1}
    )
    container = spec.factory(constructor)()
    epoch = 0
    state_epoch = 0
    members: dict[str, int] = {}
    revisions: dict[str, int] = {}
    present: set[str] = set()
    accepted: list[str] = []
    history: list[Event] = []
    for action in actions:
        if action == "reset":
            container = spec.factory(constructor)()
            epoch += 1
            state_epoch = 0
            members.clear()
            revisions.clear()
            present.clear()
            accepted.clear()
            history.clear()
        elif action in {"join-a", "join-b"}:
            member = action.removeprefix("join-")
            duplicate = member in present
            admission = epoch
            try:
                result = await invoke(spec, container, "join", member, epoch=admission)
            except ActorCallError:
                assert duplicate
            else:
                assert not duplicate
                if not members:
                    state_epoch = admission
                members.setdefault(member, 0)
                revisions[member] = revisions.get(member, -1) + 1
                present.add(member)
                assert result == len(present)
        elif action in {"leave-a", "leave-b", "stale-a", "stale-b"}:
            operation, _, member = action.partition("-")
            revision = revisions.get(member, 0)
            stale = operation == "stale"
            if stale:
                revision -= 1
            try:
                result = await invoke(
                    spec, container, "leave", member, epoch=state_epoch, revision=revision
                )
            except ActorCallError, ActorPayloadError:
                assert stale or member not in members
            else:
                assert not stale and member in members
                present.discard(member)
                assert result == len(present)
        else:
            event = Event(
                room_id="other" if action == "wrong-room" else "room",
                event_id=action,
                epoch=state_epoch,
                text="",
                tags=[],
            )
            invalid = not present or action in accepted or action == "wrong-room"
            try:
                result = await invoke(spec, container, "publish", event)
            except ActorCallError:
                assert invalid
            else:
                assert not invalid
                accepted.append(action)
                history[:] = [event]
                for member in present:
                    members[member] += 1
                assert result == Receipt(
                    "room",
                    action,
                    state_epoch,
                    tuple(
                        Delivery(member, count)
                        for member, count in members.items()
                        if member in present
                    ),
                )
        assert await invoke(spec, container, "snapshot") == Snapshot(
            "room",
            state_epoch,
            tuple(
                Delivery(member, count) for member, count in members.items() if member in present
            ),
            tuple(accepted),
            tuple(history),
            tuple(
                Membership(member, state_epoch, count, member in present, revisions[member])
                for member, count in members.items()
            ),
        )


@settings(max_examples=120, deadline=None, derandomize=True)
@given(
    actions=st.lists(
        st.sampled_from(
            (
                "join-a",
                "join-b",
                "leave-a",
                "leave-b",
                "stale-a",
                "stale-b",
                "send-0",
                "send-1",
                "wrong-room",
                "reset",
            )
        ),
        min_size=1,
        max_size=30,
    )
)
def test_room__rejections_and_resets_never_replay_and_fanout_matches_model(
    actions: list[Action],
) -> None:
    asyncio.run(check_trace(actions))


@pytest.mark.parametrize("method", ["join", "leave"])
@pytest.mark.parametrize(
    ("member", "epoch"),
    [
        ("a", True),
        ("a", False),
        ("a", "0"),
        ("a", 0.0),
        ("a", -1),
        ("a", 9_223_372_036_854_775_808),
        ("a", float("inf")),
        ("a", float("nan")),
        ("a", None),
        ("", 0),
        ("x" * 33, 0),
        (1, 0),
    ],
)
async def test_membership__invalid_boundary_never_changes_state(
    method: str,
    member: object,
    epoch: object,
) -> None:
    spec = definition(Room)
    payload = spec.constructor.encode(
        (), {"room_id": "room", "max_members": 2, "max_events": 2, "history_limit": 1}
    )
    container = spec.factory(payload)()
    assert await invoke(spec, container, "join", "a", epoch=0) == 1
    before = await invoke(spec, container, "snapshot")
    with pytest.raises((ActorCallError, ActorPayloadError)):
        await invoke(spec, container, method, member, epoch=epoch)
    assert await invoke(spec, container, "snapshot") == before


@pytest.mark.parametrize(
    "revision",
    [True, False, "0", 0.0, -1, 9_223_372_036_854_775_808, None, float("inf"), float("nan")],
)
async def test_membership__invalid_revision_never_changes_state(revision: object) -> None:
    spec = definition(Room)
    payload = spec.constructor.encode(
        (), {"room_id": "room", "max_members": 1, "max_events": 1, "history_limit": 1}
    )
    container = spec.factory(payload)()
    assert await invoke(spec, container, "join", "a", epoch=0) == 1
    before = await invoke(spec, container, "snapshot")
    with pytest.raises((ActorCallError, ActorPayloadError)):
        await invoke(spec, container, "leave", "a", epoch=0, revision=revision)
    assert await invoke(spec, container, "snapshot") == before
