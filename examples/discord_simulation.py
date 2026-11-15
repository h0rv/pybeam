import argparse
import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

import pybeam
from pybeam.actors import ActorInfo, ContextCounts, context_counts

type RoomCount = Annotated[int, Field(ge=1, le=16)]
type MemberCount = Annotated[int, Field(ge=1, le=64)]
type EventCount = Annotated[int, Field(ge=1, le=1000)]


class Config(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", validate_default=True)

    rooms: RoomCount = 4
    members: MemberCount = 8
    events: EventCount = 100
    max_inflight: Annotated[int, Field(ge=1, le=32)] = 4
    max_bytes: Annotated[int, Field(ge=4096, le=1_048_576)] = 65_536
    history: Annotated[int, Field(ge=1, le=32)] = 8
    seed: Annotated[int, Field(ge=0, le=4_294_967_295)] = 712


class Event(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    room_id: Annotated[str, Field(min_length=1, max_length=32)]
    event_id: Annotated[str, Field(min_length=1, max_length=64)]
    epoch: Annotated[int, Field(ge=0)]
    text: Annotated[str, Field(max_length=256)]
    tags: Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=4)]


@dataclass(frozen=True, slots=True)
class Delivery:
    member_id: str
    count: int


@dataclass(frozen=True, slots=True)
class Receipt:
    room_id: str
    event_id: str
    epoch: int
    deliveries: tuple[Delivery, ...]


@dataclass(frozen=True, slots=True)
class Snapshot:
    room_id: str
    epoch: int
    members: tuple[Delivery, ...]
    event_ids: tuple[str, ...]
    history: tuple[Event, ...]


@pybeam.actor
class Room:
    room_id: str
    max_members: int
    max_events: int
    history_limit: int
    epoch: int = 0
    members: dict[str, int] = field(default_factory=dict, init=False)
    event_ids: list[str] = field(default_factory=list, init=False)
    history: list[Event] = field(default_factory=list, init=False)

    async def join(self, member_id: str, *, epoch: int) -> int:
        if not member_id or len(member_id) > 32 or epoch < 0:
            raise ValueError("Invalid membership")
        if member_id in self.members:
            raise ValueError("Duplicate membership")
        if len(self.members) >= self.max_members:
            raise ValueError("Room membership capacity reached")
        if not self.members and not self.event_ids:
            self.epoch = epoch
        if epoch != self.epoch:
            raise ValueError("Wrong membership epoch")
        self.members[member_id] = 0
        return len(self.members)

    async def publish(self, event: Event) -> Receipt:
        if event.room_id != self.room_id or event.epoch != self.epoch:
            raise ValueError("Wrong room or epoch")
        if not self.members:
            raise ValueError("Explicit membership is required")
        if event.event_id in self.event_ids:
            raise ValueError("Duplicate event ID")
        if len(self.event_ids) >= self.max_events:
            raise ValueError("Room event capacity reached")
        self.event_ids.append(event.event_id)
        self.history.append(event)
        del self.history[: -self.history_limit]
        for member in self.members:
            self.members[member] += 1
        return Receipt(
            self.room_id,
            event.event_id,
            self.epoch,
            tuple(Delivery(member, count) for member, count in self.members.items()),
        )

    async def snapshot(self) -> Snapshot:
        return Snapshot(
            self.room_id,
            self.epoch,
            tuple(Delivery(member, count) for member, count in self.members.items()),
            tuple(self.event_ids),
            tuple(self.history),
        )


class Proof(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    backend: Literal["pybeam"] = "pybeam"
    room_processes: int
    client_task_limit: int
    events_before_fault: int
    fanout_receipts_before_fault: int
    events_after_explicit_rejoin: int
    fanout_receipts_after_rejoin: int
    room_identities: tuple[ActorInfo, ...]
    before: ActorInfo
    after: ActorInfo
    fresh: Snapshot
    final: Snapshot
    count_overload_rejections: int
    byte_overload_rejections: int
    contexts_before: ContextCounts
    contexts_with_rooms: ContextCounts
    contexts_after: ContextCounts
    replayed_events: Literal[0] = 0
    durability: Literal["none; restart resets state"] = "none; restart resets state"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def join_members(room: Room, count: int, epoch: int) -> None:
    for index in range(count):
        size = await room.join("member-" + str(index), epoch=epoch)
        require(size == index + 1, "Incorrect membership acknowledgement")


def check_snapshot(
    snapshot: Snapshot,
    room_id: str,
    epoch: int,
    expected_ids: tuple[str, ...],
    members: int,
    history_limit: int,
) -> None:
    require(snapshot.room_id == room_id and snapshot.epoch == epoch, "Wrong snapshot route")
    require(snapshot.event_ids == expected_ids, "Authoritative event ledger differs")
    require(
        snapshot.members
        == tuple(Delivery("member-" + str(i), len(expected_ids)) for i in range(members)),
        "Authoritative fanout differs",
    )
    require(
        tuple(event.event_id for event in snapshot.history) == expected_ids[-history_limit:],
        "Bounded history differs",
    )


async def simulate(config: Config) -> Proof:
    contexts_before = await context_counts()
    expected: list[list[str]] = [[] for _ in range(config.rooms)]
    delivered = 0
    async with pybeam.ActorGroup(
        max_children=config.rooms, max_pending=config.max_inflight, max_bytes=config.max_bytes
    ) as group:
        rooms = [
            await group.spawn(
                Room,
                room_id="room-" + str(i),
                max_members=config.members,
                max_events=config.events + 1,
                history_limit=config.history,
            )
            for i in range(config.rooms)
        ]
        for room in rooms:
            await join_members(room, config.members, 0)

        async def publish(index: int, event: Event) -> int:
            receipt = await rooms[index].publish(event)
            require(
                (receipt.room_id, receipt.event_id, receipt.epoch)
                == (event.room_id, event.event_id, event.epoch),
                "Wrong native receipt route",
            )
            require(
                receipt.deliveries
                == tuple(
                    Delivery("member-" + str(i), len(expected[index]))
                    for i in range(config.members)
                ),
                "Incorrect native fanout receipt",
            )
            return len(receipt.deliveries)

        # Python client tasks, not room processes. One request per room per batch
        # keeps the receipt oracle independent of cross-client scheduling.
        for start in range(0, config.events, min(config.max_inflight, config.rooms)):
            indices = [
                (config.seed + start + offset) % config.rooms
                for offset in range(min(config.max_inflight, config.rooms, config.events - start))
            ]
            async with asyncio.TaskGroup() as clients:
                tasks: list[asyncio.Task[int]] = []
                for offset, index in enumerate(indices):
                    event_id = "0:" + str(start + offset)
                    expected[index].append(event_id)
                    event = Event(
                        room_id="room-" + str(index),
                        event_id=event_id,
                        epoch=0,
                        text="local message",
                        tags=["simulation"],
                    )
                    tasks.append(clients.create_task(publish(index, event)))
            delivered += sum(task.result() for task in tasks)

        for index, room in enumerate(rooms):
            check_snapshot(
                await room.snapshot(),
                "room-" + str(index),
                0,
                tuple(expected[index]),
                config.members,
                config.history,
            )
        require(delivered == config.events * config.members, "Total fanout differs")
        identities = tuple([await group.info(room) for room in rooms])
        require(
            len({info.pid for info in identities}) == config.rooms,
            "Rooms do not have distinct BEAM PIDs",
        )
        contexts_with_rooms = await context_counts()
        require(
            contexts_with_rooms.live == contexts_before.live + config.rooms,
            "Each room must own one native context",
        )
        before = identities[0]
        require(
            all(info.admitted_count == 0 and info.admitted_bytes == 0 for info in identities),
            "Fault boundary is not drained",
        )
        count_rejections, byte_rejections = await overload_proof()
        await group.crash(rooms[0])
        fresh_room = await group.rejoin(rooms[0])
        after = await group.info(fresh_room)
        require(
            before.pid != after.pid and before.generation != after.generation,
            "OTP did not replace the real room process",
        )
        fresh = await fresh_room.snapshot()
        require(fresh == Snapshot("room-0", 0, (), (), ()), "Restart replayed old state")
        await join_members(fresh_room, config.members, 1)
        event = Event(
            room_id="room-0", event_id="1:0", epoch=1, text="after explicit rejoin", tags=[]
        )
        receipt = await fresh_room.publish(event)
        require(
            receipt
            == Receipt(
                "room-0",
                "1:0",
                1,
                tuple(Delivery("member-" + str(i), 1) for i in range(config.members)),
            ),
            "Recovery receipt differs",
        )
        final = await fresh_room.snapshot()
        check_snapshot(final, "room-0", 1, ("1:0",), config.members, config.history)
        for index in range(1, config.rooms):
            check_snapshot(
                await rooms[index].snapshot(),
                "room-" + str(index),
                0,
                tuple(expected[index]),
                config.members,
                config.history,
            )
    contexts_after = await context_counts()
    require(contexts_after.live == contexts_before.live, "Native owned contexts did not drain")
    return Proof(
        room_processes=config.rooms,
        client_task_limit=config.max_inflight,
        events_before_fault=config.events,
        fanout_receipts_before_fault=delivered,
        events_after_explicit_rejoin=1,
        fanout_receipts_after_rejoin=config.members,
        room_identities=identities,
        before=before,
        after=after,
        fresh=fresh,
        final=final,
        count_overload_rejections=count_rejections,
        byte_overload_rejections=byte_rejections,
        contexts_before=contexts_before,
        contexts_with_rooms=contexts_with_rooms,
        contexts_after=contexts_after,
    )


@dataclass
class EncodedEcho:
    async def dispatch(self, payload: bytes, /) -> bytes:
        return payload


async def overload_proof() -> tuple[int, int]:
    # Reserve at the public native admission boundary, before callback enqueue.
    # Aborted reservations have no effects. No unbounded fallback is used.
    from erlang.actors import ActorOverloaded, DataActorGroup

    async with DataActorGroup(max_children=1) as group:
        room = await group.spawn("capacity", EncodedEcho, capacity=1, max_bytes=4096)
        ticket = room.reserve(b"x")
        try:
            try:
                room.reserve(b"x")
            except ActorOverloaded:
                count = 1
            else:
                raise AssertionError("Native count admission did not reject")
            info = await room.info()
            require(
                (info.admitted_count, info.admitted_bytes) == (1, 1),
                "Native admission accounting differs",
            )
        finally:
            ticket.abort()
        try:
            room.reserve(b"x" * 4097)
        except ActorOverloaded:
            size = 1
        else:
            raise AssertionError("Native encoded-byte admission did not reject")
        info = await room.info()
        require((info.admitted_count, info.admitted_bytes) == (0, 0), "Admission leaked")
    return count, size


def parse_config() -> Config:
    parser = argparse.ArgumentParser(
        description="Local Discord-style BEAM room simulation",
        epilog="This run faults after drain. An admitted failed call can have effects. "
        "Calls are never automatically replayed. Restart resets state, not durable storage.",
    )
    parser.add_argument("--config", default="{}", help="Strict finite configuration as JSON")
    args = parser.parse_args()
    return Config.model_validate_json(args.config)


async def main() -> None:
    proof = await simulate(parse_config())
    print(proof.model_dump_json())


if __name__ == "__main__":
    pybeam.run(main)
