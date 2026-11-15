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

To replace a failed actor, see [Supervision](supervision.md).
