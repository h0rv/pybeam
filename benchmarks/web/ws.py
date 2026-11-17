import argparse
import asyncio
import errno
import resource
import sys
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.protocol import State


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["open", "sample", "close"]
    target: int = Field(default=0, ge=0, le=50000)


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    held: int
    errors: list[str] = Field(default_factory=list)
    latency_ms: list[float] = Field(default_factory=list)


def ensure_client_fds(target: int) -> None:
    if target == 0:
        return
    required = target + 64
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= required:
        return
    if hard != resource.RLIM_INFINITY and hard < required:
        raise RuntimeError(
            "Client-side file descriptor limit cannot cover "
            + str(target)
            + " connections plus 64 descriptors of headroom: hard limit "
            + str(hard)
        )
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (required, hard))
    except (OSError, ValueError) as error:
        raise RuntimeError("Client-side file descriptor limit could not be raised") from error


@dataclass(slots=True)
class Pool:
    url: str
    source: str
    sockets: list[ClientConnection] = field(default_factory=list)
    offset: int = 0

    async def open_one(self, errors: list[str]) -> None:
        try:
            socket = await connect(
                self.url,
                local_addr=(self.source, 0),
                compression=None,
                ping_interval=None,
                open_timeout=5,
                close_timeout=1,
                max_queue=1,
            )
            self.sockets.append(socket)
        except (OSError, TimeoutError, InvalidHandshake) as error:
            if isinstance(error, OSError) and error.errno in (errno.EMFILE, errno.ENFILE):
                raise RuntimeError("Client-side file descriptor exhaustion") from error
            errors.append(type(error).__name__ + ": " + str(error))

    async def ramp(self, target: int) -> Reply:
        ensure_client_fds(target)
        errors: list[str] = []
        while len(self.sockets) < target and not errors:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(64, target - len(self.sockets))):
                    group.create_task(self.open_one(errors))
        return Reply(held=self.held(), errors=errors)

    def held(self) -> int:
        return sum(socket.state is State.OPEN for socket in self.sockets)

    async def echo(self, socket: ClientConnection, reply: Reply) -> None:
        try:
            start = time.perf_counter_ns()
            async with asyncio.timeout(3):
                await socket.send("echo-0123456789")
                value = await socket.recv()
            if value != "echo-0123456789":
                reply.errors.append("incorrect echo")
            else:
                reply.latency_ms.append((time.perf_counter_ns() - start) / 1_000_000)
        except (OSError, TimeoutError, ConnectionClosed) as error:
            reply.errors.append(type(error).__name__ + ": " + str(error))

    async def sample(self) -> Reply:
        reply = Reply(held=self.held())
        size = len(self.sockets)
        if size:
            indices = [
                (index * size // min(size, 100) + self.offset) % size
                for index in range(min(size, 100))
            ]
            async with asyncio.TaskGroup() as group:
                for index in indices:
                    group.create_task(self.echo(self.sockets[index], reply))
            self.offset += 1
        reply.held = self.held()
        return reply

    async def close(self) -> None:
        for start in range(0, len(self.sockets), 256):
            async with asyncio.TaskGroup() as group:
                for socket in self.sockets[start : start + 256]:
                    group.create_task(socket.close())


async def worker(url: str, source: str) -> None:
    pool = Pool(url, source)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = Command.model_validate_json(line)
            match command.action:
                case "open":
                    reply = await pool.ramp(command.target)
                case "sample":
                    reply = await pool.sample()
                case "close":
                    break
            print(reply.model_dump_json(), flush=True)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    asyncio.run(worker(str(args.url), str(args.source)))


if __name__ == "__main__":
    main()
