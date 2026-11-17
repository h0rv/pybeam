# background.py
import asyncio

from background_body import watch

import pybeam
from pybeam.services.background import ServiceSlot


async def main() -> None:
    slot = ServiceSlot(capacity=1)
    ready = asyncio.Event()
    service = slot.start(watch, ready)
    try:
        await ready.wait()
    finally:
        await service.stop()
    print(service.state)


if __name__ == "__main__":
    pybeam.run(main)
