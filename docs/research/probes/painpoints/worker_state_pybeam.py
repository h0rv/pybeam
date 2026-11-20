# Rerun: ~/painpoint-venv/bin/python docs/research/probes/painpoints/worker_state_pybeam.py
import asyncio
import json

from pybeam.integrations.hornbeam import Beam, run

INCREMENTS = 400
SUBSCRIBERS = 8


async def main() -> None:
    beam = await Beam.connect()

    async def subscriber(ready: asyncio.Event) -> bool:
        async with beam.subscribe("updates", max_bytes=4096) as inbox:
            ready.set()
            try:
                async with asyncio.timeout(5):
                    return await inbox.receive() == b"update"
            except TimeoutError:
                return False

    async with asyncio.timeout(10), asyncio.TaskGroup() as listeners:
        ready = [asyncio.Event() for _ in range(SUBSCRIBERS)]
        subscribers = [listeners.create_task(subscriber(event)) for event in ready]
        for event in ready:
            await event.wait()
        async with asyncio.TaskGroup() as group:
            increments = [
                group.create_task(beam.incr("visits", namespace="painpoints"))
                for _ in range(INCREMENTS)
            ]
        await beam.publish("updates", b"update")
    print(
        json.dumps(
            {
                "case": "state",
                "increments": INCREMENTS,
                "highest_count": max(task.result() for task in increments),
                "notified": sum(task.result() for task in subscribers),
                "subscribers": SUBSCRIBERS,
            }
        )
    )


if __name__ == "__main__":
    run(main)
