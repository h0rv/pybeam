# Supervision

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

This prints `5`, then `1`. `crash` deliberately kills the child. OTP restarts it from its constructor, without the old state. `rejoin` returns a reference to the replacement. The old reference does not follow it, and no method is replayed.

`stop` removes the replacement. Leaving the group stops any remaining children.
