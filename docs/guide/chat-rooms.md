# Example: chat rooms

In plain asyncio, mutating shared room dictionaries across an `await` needs a lock per room; spreading them across processes usually adds an external store and pub/sub. Here each room owns its members, recent messages and reactions, and runs one method at a time. In `post`, `sleep(0)` stands in for awaiting moderation. Twenty-four simulated clients share three rooms.

```python
# chat_rooms.py
import asyncio
from dataclasses import dataclass, field

import pybeam


@dataclass(slots=True)
class Message:
    number: int
    author: str
    text: str
    reactions: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class Snapshot:
    members: dict[str, bool]
    messages: list[Message]
    posted: int


@pybeam.actor
class Room:
    members: dict[str, bool] = field(default_factory=dict, init=False)
    messages: list[Message] = field(default_factory=list, init=False)
    posted: int = 0

    async def join(self, name: str) -> None:
        if name not in self.members and len(self.members) >= 8:
            raise ValueError("Room is full")
        self.members[name] = True

    async def leave(self, name: str) -> None:
        if name not in self.members:
            raise ValueError("Unknown member")
        self.members[name] = False

    async def post(self, name: str, text: str) -> int:
        if not self.members.get(name, False):
            raise ValueError("Join before posting")
        number = self.posted + 1
        await asyncio.sleep(0)
        self.messages.append(Message(number, name, text))
        del self.messages[:-6]
        self.posted = number
        return number

    async def react(self, name: str, number: int) -> None:
        if not self.members.get(name, False):
            raise ValueError("Join before reacting")
        for message in self.messages:
            if message.number == number:
                voters = message.reactions.setdefault("like", [])
                if name not in voters:
                    voters.append(name)
                return
        raise ValueError("Message is no longer in recent history")

    async def snapshot(self) -> Snapshot:
        return Snapshot(self.members, self.messages, self.posted)


async def client(room: Room, name: str) -> None:
    await room.join(name)
    for _ in range(5):
        await room.post(name, "Hello from " + name)


def require(condition: bool) -> None:
    if not condition:
        raise RuntimeError("Room consistency check failed")


async def main() -> None:
    async with pybeam.ActorGroup(max_children=3, max_pending=16) as actors:
        rooms = [await actors.spawn(Room) for _ in range(3)]
        async with asyncio.TaskGroup() as clients:
            for room in rooms:
                for index in range(8):
                    clients.create_task(client(room, "member-" + str(index)))

        for index, room in enumerate(rooms):
            before = await room.snapshot()
            require(before.posted == 40)
            numbers = [message.number for message in before.messages]
            require(numbers == list(range(35, 41)))
            async with asyncio.TaskGroup() as clients:
                for name in before.members:
                    clients.create_task(room.react(name, before.posted))
            for name in before.members:
                await room.leave(name)

            snapshot = await room.snapshot()
            likes = len(snapshot.messages[-1].reactions["like"])
            online = sum(snapshot.members.values())
            recent = len(snapshot.messages)
            require(recent == 6 and likes == 8 and online == 0)
            snapshot.messages.clear()
            require(len((await room.snapshot()).messages) == 6)
            print(
                "room-" + str(index),
                "posted=" + str(snapshot.posted),
                "recent=" + str(recent),
                "likes=" + str(likes),
                "online=" + str(online),
            )

        await actors.crash(rooms[0])
        restarted = await actors.rejoin(rooms[0])
        require(await restarted.snapshot() == Snapshot({}, [], 0))
        print("room-0 restarted: posted=0 recent=0 members=0")


if __name__ == "__main__":
    pybeam.run(main)
```

```console
$ uv run chat_rooms.py
room-0 posted=40 recent=6 likes=8 online=0
room-1 posted=40 recent=6 likes=8 online=0
room-2 posted=40 recent=6 likes=8 online=0
room-0 restarted: posted=0 recent=0 members=0
```

Each room retains six messages and at most eight member identities, including offline members. Reactions record each member once. Clearing a returned snapshot does not change the room. Consistency checks cover consecutive message numbers, bounded history and reaction counts, not throughput or production capacity.

## What this does not do

- Run across machines: these actors live on one node; this API cannot place them on other machines.
- Isolate Python memory: arguments and results are copied as JSON, but actors share one Python interpreter. Calls on different workers use separate loops; parallel Python CPU work requires a GIL-free build.
- Roll back failed methods: a method that raises keeps changes made before the raise.
- Restore saved state: a crashed room restarts from its constructor, empty here. There is no automatic restore. [Supervision](supervision.md) is not storage; Hornbeam's [shared store](state.md) is in-memory too.
- Implement typing indicators or WebSockets. For notifications, see [Messaging](messaging.md).
