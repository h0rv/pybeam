# Background services

Start async work, retain its handle, and wait for cleanup when stopping it.

Save this function as `background_body.py`.

```python
import asyncio


async def watch() -> None:
    try:
        await asyncio.Event().wait()
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
    service = slot.start(watch)
    try:
        await asyncio.sleep(0.01)
        print(service.state)
    finally:
        await service.stop()
    print(service.state)


if __name__ == "__main__":
    pybeam.run(main)
```

This prints `running`, `cleanup complete`, then `cancelled`. The slot admits one live service. Its entry must be an importable module-level async function, not a closure.

`stop` requests cooperative cancellation and waits for cleanup. A timeout from `wait` or `stop` does not prove the body has ended. This local API passes Python arguments directly; it does not serialize them.
