# Actors

Use an actor to own Python state and expose methods that change it.

```python
import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, amount: int) -> int:
        self.value += amount
        return self.value

    async def read(self) -> int:
        return self.value


async def main() -> None:
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter, value=10)
        print(await counter.add(2))
        print(await counter.read())


if __name__ == "__main__":
    pybeam.run(main)
```

This prints `12` twice. `spawn` constructs the actor. Call `add` to change its state and `read` to return the current value. Leaving the group stops its children.

Call methods rather than reading `counter.value`. Requests and results are serialized copies. Each actor handles calls serially, even while a method awaits.

Actor admission bounds queued plus running requests and encoded bytes, not total memory. A timeout stops waiting; it does not undo effects. Retrying a call can repeat its effects.

## Execution limits

Dedicated execution is the default (`execution=None`). To request pooled execution
from a native runtime that supports it, pass an immutable `ExecutionLimits` value.

```python
from pybeam import ActorGroup, ExecutionLimits


async def pooled() -> None:
    async with ActorGroup(
        max_children=800,
        max_pending=8,
        max_bytes=65_536,
        execution=ExecutionLimits(workers=4, capacity=32, max_bytes=1_048_576),
    ) as actors:
        counter = await actors.spawn(Counter)
        await counter.add(1)
```

`workers` bounds physical Python execution contexts, not the number of actors.
`execution.capacity` and `execution.max_bytes` bound aggregate admitted pool work
and encoded request bytes. The group's `max_pending` and `max_bytes` remain
per-actor limits. Neither byte limit bounds arbitrary Python heap allocation.
All three execution limits must be strict positive integers.

Pooled execution requires native support that is still under development. A
requested pool fails if that support is unavailable. It never falls back to
dedicated execution.

To replace a failed actor, see [Supervision](supervision.md).
