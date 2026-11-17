# background_body.py
import asyncio


async def watch(ready: asyncio.Event) -> None:
    try:
        while True:
            print("heartbeat")
            ready.set()
            await asyncio.sleep(1)
    finally:
        print("cleanup complete")
