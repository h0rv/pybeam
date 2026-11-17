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
