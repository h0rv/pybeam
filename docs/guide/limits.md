# Boundaries

A deadline stops waiting; it does not roll back completed effects.

```python
import asyncio

import pybeam


async def main() -> None:
    changes: list[str] = []
    try:
        async with asyncio.timeout(0.01):
            changes.append("sent")
            await asyncio.sleep(1)
    except TimeoutError:
        print(changes)


if __name__ == "__main__":
    pybeam.run(main)
```

This prints `['sent']`. The change happened before the timeout. The same principle applies to actor methods and external writes. Choose retries explicitly; execution is not exactly once.

Python runs in one VM with one main interpreter and its GIL. OTP processes do not isolate C-extension crashes or provide multicore Python execution.

Actor admission bounds queued plus running requests and encoded bytes, not RSS. Service state and messages are not durable. Supervision replaces processes; applications own persistence and authorization.
