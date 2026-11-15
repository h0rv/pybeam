import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from examples.discord_simulation import Event, Room, Snapshot
from pybeam.actors import ActorGroup, ActorInfo
from pybeam.integrations.hornbeam.contracts import Services

ROOMS = ("general", "python", "beam", "random")
type Action = Literal["join", "leave", "send", "crash"]
type Leave = Callable[[Room, str, int], Awaitable[int]]


@dataclass(slots=True)
class RoomHandle:
    reference: Room
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class View:
    snapshot: Snapshot
    info: ActorInfo


class Demo:
    def __init__(
        self, group: ActorGroup, services: Services, *, leave: Leave | None = None
    ) -> None:
        self.group = group
        self.services = services
        self.leave = leave
        self.rooms: dict[str, RoomHandle] = {}
        self.streams = 0
        self.inflight = 0
        self.owner: str | None = None

    async def start(self) -> None:
        for room_id in ROOMS:
            reference = await self.group.spawn(
                Room, room_id=room_id, max_members=8, max_events=100, history_limit=32
            )
            self.rooms[room_id] = RoomHandle(reference)

    async def view(self, room_id: str) -> View:
        handle = self.rooms[room_id]
        async with handle.lock:
            return await self._view(handle)

    async def _view(self, handle: RoomHandle) -> View:
        return View(await handle.reference.snapshot(), await self.group.info(handle.reference))

    async def change(
        self, room_id: str, member: str, generation: int, action: Action, text: str, event_id: str
    ) -> None:
        handle = self.rooms[room_id]
        async with handle.lock:
            view = await self._view(handle)
            if view.info.generation != generation:
                raise ValueError(
                    "Room generation changed. Read the fresh stream and explicitly join."
                )
            joined = any(item.member_id == member for item in view.snapshot.members)
            match action:
                case "join":
                    await handle.reference.join(member, epoch=generation)
                case "leave":
                    if self.leave is None:
                        raise ValueError("Native Room.leave is not available in this revision.")
                    await self.leave(handle.reference, member, generation)
                case "send":
                    if not joined:
                        raise ValueError("Join this room generation before sending.")
                    await handle.reference.publish(
                        Event(
                            room_id=room_id,
                            event_id=event_id,
                            epoch=generation,
                            text=text,
                            tags=[member],
                        )
                    )
                case "crash":
                    if member != self.owner:
                        raise ValueError("Only the first local browser owns crash diagnostics.")
                    await self.group.crash(handle.reference)
                    handle.reference = await self.group.rejoin(handle.reference)
                    fresh = await self._view(handle)
                    if fresh.snapshot.members or fresh.snapshot.history or fresh.snapshot.event_ids:
                        raise RuntimeError("Native restart did not return empty state")
                    if fresh.info.pid == view.info.pid:
                        raise RuntimeError("Native child PID did not change")
                case _:
                    raise ValueError("Unsupported room action")
            await self.services.publish("discord:" + room_id, b"changed")
