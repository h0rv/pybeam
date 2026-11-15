import asyncio
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from typing import NamedTuple

from pybeam.services.background import (
    ServiceBusyError,
    ServiceRef,
    ServiceSlot,
    ServiceState,
)


@dataclass
class Work:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    sockets: tuple[socket.socket, socket.socket] | None = None
    received: bytes = b""

    def closed_sockets(self) -> int:
        if self.sockets is None:
            return 0
        return sum(sock.fileno() == -1 for sock in self.sockets)


class Outcome(NamedTuple):
    completed: ServiceState
    failed: ServiceState
    stopped: ServiceState
    work_after_starter_returned: bytes
    admission_rejected: bool
    closed_sockets: int
    pending_tasks: int


class ExpectedFailure(Exception):
    pass


async def consume(work: Work) -> None:
    reader, writer = socket.socketpair()
    work.sockets = (reader, writer)
    try:
        reader.setblocking(False)
        writer.setblocking(False)
        work.started.set()
        await work.proceed.wait()
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(writer, b"x")
        work.received = await loop.sock_recv(reader, 1)
    finally:
        reader.close()
        writer.close()


async def initiate(slot: ServiceSlot, work: Work) -> ServiceRef:
    return slot.start(consume, work)


async def fail() -> None:
    raise ExpectedFailure("service failure is observable")


async def run() -> Outcome:
    slot = ServiceSlot(capacity=1)
    completed_work = Work()
    stopped_work = Work()
    try:
        async with asyncio.TaskGroup() as group:
            starter = group.create_task(initiate(slot, completed_work))
        ref = starter.result()
        async with asyncio.timeout(1):
            await completed_work.started.wait()

        try:
            slot.start(fail)
        except ServiceBusyError:
            admission_rejected = True
        else:
            raise RuntimeError("a live service must occupy its slot")

        try:
            await ref.wait(timeout=0)
        except TimeoutError:
            if ref.state != "running":
                raise RuntimeError("a wait timeout must leave the service running") from None
        else:
            raise RuntimeError("service work must wait for its event")

        completed_work.proceed.set()
        await ref.wait(timeout=1)
        completed_state = ref.state

        failed = slot.start(fail)
        try:
            await failed.wait(timeout=1)
        except ExpectedFailure:
            failed_state = failed.state
        else:
            raise RuntimeError("service failure was not observed")

        stopped = slot.start(consume, stopped_work)
        async with asyncio.timeout(1):
            await stopped_work.started.wait()
        await stopped.stop(timeout=1)
        return Outcome(
            completed=completed_state,
            failed=failed_state,
            stopped=stopped.state,
            work_after_starter_returned=completed_work.received,
            admission_rejected=admission_rejected,
            closed_sockets=completed_work.closed_sockets() + stopped_work.closed_sockets(),
            pending_tasks=len(asyncio.all_tasks() - {asyncio.current_task()}),
        )
    finally:
        with suppress(ExpectedFailure):
            await slot.stop(timeout=1)
