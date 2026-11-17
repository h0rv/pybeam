# Supervision

Restart a failed actor and get a reference to the replacement.

```python
# supervision.py
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

```console
$ uv run supervision.py
5
1
```

`crash` kills the actor on purpose. Its group restarts it from the constructor, so the old state is gone. `rejoin` returns a reference to the replacement; the old reference does not follow it, and no call is replayed.

`stop` stops the replacement. Leaving the group stops any remaining actors.
