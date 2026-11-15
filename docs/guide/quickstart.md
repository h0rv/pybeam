# Quickstart

```python
import asyncio

import pybeam


async def main() -> None:
    await asyncio.sleep(0.01)
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
```

## Run async Python

Save this [example](../../examples/hello.py) as `main.py` and run `python main.py`. It prints `Hello from Python`.

Write your application in the async `main` function. Use `await` and Python libraries there. `pybeam.run(main)` owns runtime startup and shutdown, so you do not create or manage a VM yourself. Pass the function, not `main()`.

## Add primitives when needed

You do not need an actor to run Python on BEAM. Choose primitives for particular parts of your application.

- Use [background jobs and tasks](primitives.md#background-jobs-and-tasks) for work whose completion and cleanup need an owner.
- Use [channels](primitives.md#channels) to exchange typed messages between producers and consumers.
- Use [actors](primitives.md#actors) when callers need methods on one state owner. The [Counter example](../../examples/actors/main.py) shows `@pybeam.actor`, awaited calls, and `ActorGroup`.
- Use [supervision](primitives.md#supervision-and-lifetimes) to define process lifetimes and replacement after failure.

For HTTP handlers and live updates, keep your framework and use the [web integrations](web.md).
