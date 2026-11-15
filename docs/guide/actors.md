# Actors

Give state one owner and call its typed async methods.

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

Call methods rather than reading `counter.value`. Requests and results are serialized copies. Each actor handles calls serially, even while a method awaits. See [supervision](supervision.md) for replacement after failure.
