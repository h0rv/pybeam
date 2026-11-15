# Background services

Run a periodic heartbeat and wait for cleanup when stopping it.

Save this function as `background_body.py`. The service entry must be a module-level async function in an importable module, so it can be found by module and name. A closure cannot be used.

```python
import asyncio


async def watch(ready: asyncio.Event) -> None:
    try:
        while True:
            print("heartbeat")
            ready.set()
            await asyncio.sleep(1)
    finally:
        print("cleanup complete")
```

Save this entry file beside it and run it with Python.

```python
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
```

The service prints `heartbeat` once per second until stopped. The entry waits for the first heartbeat, then stops it. You will see `cleanup complete`, then `cancelled`. The event confirms that work started without relying on a timed sleep.

The slot admits one live service. Retain its handle and call `stop` in `finally`. `stop` requests cooperative cancellation and waits for cleanup. A timeout from `wait` or `stop` does not prove the body has ended. Arguments are passed directly, not serialized.
