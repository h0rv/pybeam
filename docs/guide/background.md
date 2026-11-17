# Background services

Run a long-lived loop and stop it cleanly.

A service must be a module-level async function, not a closure or lambda.

```python
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
```

```python
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
```

```console
$ uv run background.py
heartbeat
cleanup complete
cancelled
```

`main` waits for the first heartbeat and then stops the service. Waiting on the event, instead of sleeping, proves the loop started.

`ServiceSlot(capacity=1)` runs at most one service at a time. Keep the handle `start` returns and call `stop` in `finally`. `stop` cancels the loop and waits for its `finally` block to finish. If `stop` times out, the loop may still be running.
