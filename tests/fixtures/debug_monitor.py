import asyncio
from pathlib import Path

from pybeam import run
from pybeam.actors import ActorGroup, actor
from pybeam.codec import Json
from pybeam.queues import DurableQueue
from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor


@actor
class Counter:
    async def wait(self) -> None:
        await asyncio.sleep(3600)


async def parked(value: int) -> int:
    await asyncio.sleep(3600)
    return value


async def completed(value: int) -> int:
    return value


async def main() -> None:
    queue = DurableQueue("monitor", max_items=4)
    await queue.put(b"one")
    await queue.put(b"two")
    async with (
        queue.get(),
        ActorGroup(max_pending=2) as actors,
        TaskSupervisor(TaskLimits(capacity=3, max_bytes=4096, shutdown_ms=1000)) as tasks,
        TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as retained,
        TaskSupervisor(TaskLimits(capacity=2, max_bytes=1, shutdown_ms=1000)) as byte_limited,
    ):
        counter = await actors.spawn(Counter)
        await tasks.start(TaskFunction(parked, Json(int), Json(int)), 7)
        await retained.start(TaskFunction(completed, Json(int), Json(int)), 7)
        await byte_limited.start(TaskFunction(parked, Json(int), Json(int)), 7)
        pending = asyncio.create_task(counter.wait(), name="counter-wait")
        try:
            await asyncio.sleep(0.05)
            await asyncio.to_thread(Path("monitor.ready").touch)
            await asyncio.Event().wait()
        finally:
            pending.cancel()


if __name__ == "__main__":
    run(main)
