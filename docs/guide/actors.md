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

This prints `12` twice. `spawn` constructs the actor; `add` changes its state; `read` returns the current value. Leaving the group stops its children.

Call methods rather than reading `counter.value`. Requests and results are serialized copies. Each actor handles calls serially, even while a method awaits.

Actor admission bounds queued plus running requests and encoded bytes, not total memory. A timeout stops waiting; it does not undo effects. Retrying a call can repeat its effects.

## After a failure

Replace a failed actor and acquire its new reference.

```python
import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, amount: int) -> int:
        self.value += amount
        return self.value


async def main() -> None:
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter)
        print(await counter.add(5))
        await actors.crash(counter)
        replacement = await actors.rejoin(counter)
        print(await replacement.add(1))
        await actors.stop(replacement)


if __name__ == "__main__":
    pybeam.run(main)
```

This prints `5`, then `1`. `crash` deliberately kills the child. OTP restarts it from its constructor, without the old state. `rejoin` returns a reference to the replacement. The old reference does not follow it, and no method is replayed. `stop` removes the replacement; group exit stops any remaining children.
