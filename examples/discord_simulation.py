import argparse
import asyncio
from dataclasses import dataclass, field, replace
from typing import Literal

import pybeam
from pybeam.actors import ActorInfo, ContextCounts, context_counts
from pybeam.codec import Json

_MAX_INT64 = 9_223_372_036_854_775_807


def _bounded(name: str, value: int, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + " must be an integer from " + str(low) + " to " + str(high))


def _text(name: str, value: str, low: int, high: int) -> None:
    if not low <= len(value) <= high:
        raise ValueError(name + " must have " + str(low) + " to " + str(high) + " characters")


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    rooms: int = 4
    members: int = 8
    events: int = 100
    max_inflight: int = 4
    max_bytes: int = 65_536
    history: int = 8
    seed: int = 712

    def __post_init__(self) -> None:
        _bounded("rooms", self.rooms, 1, 16)
        _bounded("members", self.members, 1, 64)
        _bounded("events", self.events, 1, 1000)
        _bounded("max_inflight", self.max_inflight, 1, 32)
        _bounded("max_bytes", self.max_bytes, 4096, 1_048_576)
        _bounded("history", self.history, 1, 32)
        _bounded("seed", self.seed, 0, 4_294_967_295)


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    room_id: str
    event_id: str
    epoch: int
    text: str
    tags: list[str]

    def __post_init__(self) -> None:
        _text("room_id", self.room_id, 1, 32)
        _text("event_id", self.event_id, 1, 64)
        _bounded("epoch", self.epoch, 0, _MAX_INT64)
        _text("text", self.text, 0, 256)
        if len(self.tags) > 4:
            raise ValueError("tags must have at most 4 items")
        for tag in self.tags:
            _text("tag", tag, 0, 32)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class MembershipInput:
    member_id: str
    epoch: int
    revision: int = 0

    def __post_init__(self) -> None:
        _text("member_id", self.member_id, 1, 32)
        _bounded("epoch", self.epoch, 0, _MAX_INT64)
        _bounded("revision", self.revision, 0, _MAX_INT64)


@dataclass(frozen=True, slots=True)
class Membership:
    member_id: str
    epoch: int
    count: int
    present: bool
    revision: int = 0


@dataclass(frozen=True, slots=True)
class Snapshot:
    room_id: str
    epoch: int
    members: tuple[Delivery, ...]
    event_ids: tuple[str, ...]
    history: tuple[Event, ...]
    ledger: tuple[Membership, ...] = ()


@pybeam.actor
class Room:
    room_id: str
    max_members: int
    max_events: int
    history_limit: int
    epoch: int = 0
    members: dict[str, Membership] = field(default_factory=dict, init=False)
    event_ids: list[str] = field(default_factory=list, init=False)
    history: list[Event] = field(default_factory=list, init=False)

    async def join(self, member_id: str, *, epoch: int) -> int:
        """Return presence count; keep epoch as the room's publish epoch.

        The first member establishes the room epoch. A departed member rejoins
        in that same epoch with an incremented authoritative membership revision.
        Snapshot.ledger exposes that revision for subsequent leave forms.
        Counts and departed-member tombstones last for this native generation.
        max_members bounds distinct identities, including departed identities.
        """
        request = MembershipInput(member_id=member_id, epoch=epoch)
        if self.members and request.epoch != self.epoch:
            raise ValueError("Wrong membership epoch")
        previous = self.members.get(request.member_id)
        if previous is not None:
            if previous.present:
                raise ValueError("Duplicate membership")
            if previous.revision >= 9_223_372_036_854_775_807:
                raise ValueError("Membership revision exhausted")
            membership = replace(previous, revision=previous.revision + 1, present=True)
        else:
            if len(self.members) >= self.max_members:
                raise ValueError("Room membership capacity reached")
            membership = Membership(request.member_id, request.epoch, 0, True)
        if not self.members:
            self.epoch = request.epoch
        self.members[request.member_id] = membership
        return sum(member.present for member in self.members.values())

    async def leave(self, member_id: str, *, epoch: int, revision: int = 0) -> int:
        """Return remaining presence count without deleting receipts or history.

        Epoch is the existing room epoch, not a membership lease. Revision defaults
        to zero for first admission. Submit the revision captured in the leave form
        from Snapshot.ledger, never the latest revision looked up at execution time.
        Matching repeated leaves are idempotent. Unknown members or mismatched
        epochs/revisions fail without mutation. Native references guard generation.
        """
        request = MembershipInput(member_id=member_id, epoch=epoch, revision=revision)
        membership = self.members.get(request.member_id)
        if membership is None:
            raise ValueError("Unknown membership")
        if request.epoch != self.epoch:
            raise ValueError("Wrong membership epoch")
        if request.revision != membership.revision:
            raise ValueError("Wrong membership revision")
        self.members[request.member_id] = replace(membership, present=False)
        return sum(member.present for member in self.members.values())

    async def publish(self, event: Event) -> Receipt:
        if event.room_id != self.room_id or event.epoch != self.epoch:
            raise ValueError("Wrong room or epoch")
        if not any(member.present for member in self.members.values()):
            raise ValueError("Explicit membership is required")
        if event.event_id in self.event_ids:
            raise ValueError("Duplicate event ID")
        if len(self.event_ids) >= self.max_events:
            raise ValueError("Room event capacity reached")
        self.event_ids.append(event.event_id)
        self.history.append(event)
        del self.history[: -self.history_limit]
        for member_id, member in self.members.items():
            if member.present:
                self.members[member_id] = replace(member, count=member.count + 1)
        return Receipt(
            self.room_id,
            event.event_id,
            self.epoch,
            tuple(
                Delivery(member.member_id, member.count)
                for member in self.members.values()
                if member.present
            ),
        )

    async def snapshot(self) -> Snapshot:
        return Snapshot(
            self.room_id,
            self.epoch,
            tuple(
                Delivery(member.member_id, member.count)
                for member in self.members.values()
                if member.present
            ),
            tuple(self.event_ids),
            tuple(self.history),
            tuple(self.members.values()),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Proof:
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
        snapshot.ledger
        == tuple(
            Membership("member-" + str(i), epoch, len(expected_ids), True) for i in range(members)
        ),
        "Authoritative historical ledger differs",
    )
    require(
        tuple(event.event_id for event in snapshot.history) == expected_ids[-history_limit:],
        "Bounded history differs",
    )


CONFIG = Json(Config)
PROOF = Json(Proof)


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
            contexts_before.live <= contexts_with_rooms.live <= contexts_before.live + config.rooms,
            "Shared worker growth exceeded room concurrency",
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
    require(contexts_after == contexts_with_rooms, "Drained groups changed the shared worker pool")
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
    return CONFIG.decode(args.config)


async def main() -> None:
    proof = await simulate(parse_config())
    print(PROOF.encode(proof).decode())


if __name__ == "__main__":
    pybeam.run(main)
