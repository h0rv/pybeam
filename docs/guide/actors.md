# Actors

Use an actor to keep state that many tasks update.

```python
# actors.py
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

```console
$ uv run actors.py
12
12
```

`spawn` creates the actor with `value=10`. `add` changes its state and `read` returns it. Leaving the `ActorGroup` stops its actors.

An actor runs one method call at a time, even while a method awaits, so concurrent callers never need a lock. Call methods instead of reading `counter.value`: arguments and results are copied.

Each actor accepts a limited number of waiting calls. A timeout stops waiting; it does not undo the call, and retrying can repeat its effects.

To restart an actor that fails, see [Supervision](supervision.md).
