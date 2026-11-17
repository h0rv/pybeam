import asyncio

import pytest

from examples.docs.chat_rooms import Room, Snapshot, client
from pybeam.actors.classes import Container, Route, definition
from pybeam.actors.codec import ActorCallError


class SerialActor:
    child_id = "room"

    def __init__(self, container: Container) -> None:
        self.container = container
        self.lock = asyncio.Lock()

    async def call(self, payload: bytes, /) -> bytes:
        async with self.lock:
            return await self.container.dispatch(payload)

    async def rejoin(self) -> SerialActor:
        return self


@pytest.fixture
def room() -> Room:
    spec = definition(Room)
    container = spec.factory(spec.constructor.encode((), {}))()
    return spec.reference(Room, Route(SerialActor(container), spec.methods, Room))


async def test_room__starts_empty(room: Room) -> None:
    assert await room.snapshot() == Snapshot({}, [], 0)


async def test_room__bounds_history_and_copies_nested_snapshots(room: Room) -> None:
    async with asyncio.TaskGroup() as clients:
        for index in range(8):
            clients.create_task(client(room, "member-" + str(index)))
    snapshot = await room.snapshot()
    assert snapshot.posted == 40
    assert [message.number for message in snapshot.messages] == list(range(35, 41))
    for name in snapshot.members:
        await room.react(name, 40)
        await room.react(name, 40)
        await room.leave(name)
    snapshot = await room.snapshot()
    assert not any(snapshot.members.values())
    assert len(snapshot.messages[-1].reactions["like"]) == 8
    snapshot.members.clear()
    snapshot.messages[-1].reactions["like"].clear()
    fresh = await room.snapshot()
    assert len(fresh.members) == 8
    assert len(fresh.messages[-1].reactions["like"]) == 8
    await room.join("member-0")
    assert (await room.snapshot()).members["member-0"]


async def test_room__rejects_invalid_members_and_evicted_messages(room: Room) -> None:
    with pytest.raises(ActorCallError, match="Join before posting"):
        await room.post("unknown", "Hello")
    with pytest.raises(ActorCallError, match="Join before reacting"):
        await room.react("unknown", 1)
    with pytest.raises(ActorCallError, match="Unknown member"):
        await room.leave("unknown")
    for index in range(8):
        await room.join(str(index))
    with pytest.raises(ActorCallError, match="Room is full"):
        await room.join("extra")
    for _ in range(7):
        await room.post("0", "Hello")
    with pytest.raises(ActorCallError, match="no longer in recent history"):
        await room.react("0", 1)
    await room.leave("0")
    with pytest.raises(ActorCallError, match="Join before posting"):
        await room.post("0", "Hello")
    assert (await room.snapshot()).posted == 7
