# Supervision

Replace a failed actor with fresh state and explicitly acquire its new generation.

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

`crash` deliberately kills this child for diagnosis. OTP restarts the permanent child from its constructor. `rejoin` returns a reference to that replacement.

The old state is not restored, and the failed generation is not silently followed. No method is replayed. `stop` removes the child from active use; group exit cleans up the remaining children.
