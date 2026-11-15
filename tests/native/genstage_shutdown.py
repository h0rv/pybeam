import asyncio

import pybeam
from examples.genstage_callbacks import process
from pybeam.integrations.genstage import enable, stream

enable()


async def main() -> None:
    import erlang

    async def consume() -> None:
        try:
            async with stream(16, process, action="slow") as events:
                print("SHUTDOWN_READY", flush=True)
                async for _ in events:
                    pass
        finally:
            live = await erlang.async_call("pybeam_genstage", b"contexts")
            print("SHUTDOWN_CONTEXTS", live, flush=True)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(consume())
        await asyncio.Event().wait()


pybeam.run(main)
