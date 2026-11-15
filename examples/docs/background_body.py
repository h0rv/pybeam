import asyncio


async def watch() -> None:
    try:
        await asyncio.Event().wait()
    finally:
        print("cleanup complete")
