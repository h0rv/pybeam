import asyncio

from background_body import watch

import pybeam
from pybeam.services.background import ServiceSlot


async def main() -> None:
    slot = ServiceSlot(capacity=1)
    service = slot.start(watch)
    try:
        await asyncio.sleep(0.01)
        print(service.state)
    finally:
        await service.stop()
    print(service.state)


if __name__ == "__main__":
    pybeam.run(main)
