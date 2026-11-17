import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field

type Source = Callable[[str], AbstractAsyncContextManager[AsyncIterable[bytes]]]


@dataclass(slots=True)
class Room:
    listeners: set[asyncio.Queue[bytes]] = field(default_factory=set)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    def close(self) -> None:
        for listener in self.listeners:
            listener.shutdown(immediate=True)


@dataclass(slots=True)
class Hub:
    source: Source
    tasks: asyncio.TaskGroup
    depth: int = 256
    rooms: dict[str, Room] = field(default_factory=dict)

    async def pump(self, topic: str, room: Room) -> None:
        try:
            async with self.source(topic) as subscription:
                room.ready.set()
                async for payload in subscription:
                    for listener in tuple(room.listeners):
                        try:
                            listener.put_nowait(payload)
                        except asyncio.QueueFull:
                            listener.shutdown(immediate=True)
                            room.listeners.discard(listener)
        finally:
            room.close()
            room.ready.set()
            if self.rooms.get(topic) is room:
                del self.rooms[topic]

    @asynccontextmanager
    async def join(self, topic: str) -> AsyncGenerator[asyncio.Queue[bytes]]:
        room = self.rooms.get(topic)
        if room is None:
            room = self.rooms[topic] = Room()
            room.task = self.tasks.create_task(self.pump(topic, room))
        listener = asyncio.Queue[bytes](self.depth)
        room.listeners.add(listener)
        try:
            await room.ready.wait()
            yield listener
        finally:
            room.listeners.discard(listener)
            if not room.listeners and room.task is not None:
                room.task.cancel()
                if self.rooms.get(topic) is room:
                    del self.rooms[topic]
