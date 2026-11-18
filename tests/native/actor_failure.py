import asyncio

import erlang

from pybeam import ActorGroup, actor


@actor
class Counter:
    value: int = 0

    async def crash(self, pid: str) -> None:
        await asyncio.sleep(0.25)
        await erlang.async_call("failure_probe_crash", pid)

    async def add(self) -> int:
        self.value += 1
        return self.value


async def observe(call: asyncio.Task[object]) -> str:
    done, _ = await asyncio.wait({call}, timeout=2)
    if not done:
        call.cancel()
        return "timeout"
    try:
        call.result()
    except (Exception, asyncio.CancelledError) as error:
        print("failure-detail", type(error).__name__, str(error), flush=True)
        return type(error).__name__
    return "returned"


async def verify_group(group: ActorGroup) -> None:
    counter = await group.spawn(Counter)
    pid = (await group.info(counter)).pid
    first: asyncio.Task[object] = asyncio.create_task(counter.crash(pid))
    async with asyncio.timeout(2):
        while (await group.info(counter)).admitted_count != 1:  # noqa: ASYNC110
            await asyncio.sleep(0)
    second: asyncio.Task[object] = asyncio.create_task(counter.add())
    async with asyncio.timeout(2):
        while (await group.info(counter)).admitted_count != 2:  # noqa: ASYNC110
            await asyncio.sleep(0)
    first_result = await observe(first)
    second_result = await observe(second)
    print("failure-probe", first_result, second_result, flush=True)
    assert first_result == "RuntimeError"
    assert second_result == "RuntimeError"


async def verify() -> bool:
    async with ActorGroup() as group:
        await verify_group(group)
    return True
