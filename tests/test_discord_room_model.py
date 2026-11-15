import asyncio
from typing import Literal

from hypothesis import given, settings
from hypothesis import strategies as st

from examples.discord_simulation import Delivery, Event, Receipt, Room, Snapshot
from pybeam.actors.classes import Container, Definition, definition
from pybeam.actors.codec import ActorCallError, decode_reply

type Action = Literal["join-a", "join-b", "send-0", "send-1", "wrong-room", "reset"]


async def invoke(
    spec: Definition, container: Container, name: str, *args: object, **kwargs: object
) -> object:
    index, method = next(
        (i, method) for i, method in enumerate(spec.methods) if method.name == name
    )
    payload = index.to_bytes(4) + method.arguments.encode(args, kwargs)
    return method.decode(decode_reply(await container.dispatch(payload)))


async def check_trace(actions: list[Action]) -> None:
    # This checks business transitions through the real JSON dispatcher in an
    # ordinary interpreter. Native process ownership is tested separately.
    spec = definition(Room)
    constructor = spec.constructor.encode(
        (), {"room_id": "room", "max_members": 2, "max_events": 2, "history_limit": 1}
    )
    container = spec.factory(constructor)()
    epoch = 0
    state_epoch = 0
    members: dict[str, int] = {}
    accepted: list[str] = []
    history: list[Event] = []
    for action in actions:
        if action == "reset":
            container = spec.factory(constructor)()
            epoch += 1
            state_epoch = 0
            members.clear()
            accepted.clear()
            history.clear()
        elif action in {"join-a", "join-b"}:
            member = action.removeprefix("join-")
            duplicate = member in members
            try:
                result = await invoke(spec, container, "join", member, epoch=epoch)
            except ActorCallError:
                assert duplicate
            else:
                assert not duplicate
                members[member] = 0
                state_epoch = epoch
                assert result == len(members)
        else:
            event = Event(
                room_id="other" if action == "wrong-room" else "room",
                event_id=action,
                epoch=epoch,
                text="",
                tags=[],
            )
            invalid = not members or action in accepted or action == "wrong-room"
            try:
                result = await invoke(spec, container, "publish", event)
            except ActorCallError:
                assert invalid
            else:
                assert not invalid
                accepted.append(action)
                history[:] = [event]
                for member in members:
                    members[member] += 1
                assert result == Receipt(
                    "room",
                    action,
                    epoch,
                    tuple(Delivery(member, count) for member, count in members.items()),
                )
        snapshot = await invoke(spec, container, "snapshot")
        # Rejected actions and resets never add or replay an event. Every accepted
        # event adds exactly one delivery to each current member.
        assert snapshot == Snapshot(
            "room",
            state_epoch,
            tuple(Delivery(member, count) for member, count in members.items()),
            tuple(accepted),
            tuple(history),
        )


@settings(max_examples=80, deadline=None, derandomize=True)
@given(
    actions=st.lists(
        st.sampled_from(("join-a", "join-b", "send-0", "send-1", "wrong-room", "reset")),
        min_size=1,
        max_size=20,
    )
)
def test_room__rejections_and_resets_never_replay_and_fanout_matches_model(
    actions: list[Action],
) -> None:
    asyncio.run(check_trace(actions))
