# Async Python

Use ordinary Python concurrency inside the managed runtime.

```python
import asyncio

import pybeam


async def uppercase(text: str) -> str:
    await asyncio.sleep(0)
    return text.upper()


async def main() -> None:
    async with asyncio.TaskGroup() as work:
        first = work.create_task(uppercase("first"))
        second = work.create_task(uppercase("second"))
    print(first.result(), second.result())


if __name__ == "__main__":
    pybeam.run(main)
```

This prints `FIRST SECOND`. The `TaskGroup` waits for both functions before results are read. These are Python asyncio tasks, not separate OTP worker processes.

`pybeam.run(main)` owns runtime startup and shutdown. Use [actors](actors.md) when work also needs a state owner, or a [background service](background.md) when it needs an explicit service lifetime.
