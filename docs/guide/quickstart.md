# Quickstart

```python
import asyncio
import itertools

import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, n: int) -> int:
        self.value += n
        return self.value


async def main() -> None:
    async with pybeam.ActorGroup() as actors:
        first = await actors.spawn(Counter, value=0)
        print(await first.add(1))
        counters: list[Counter] = await asyncio.gather(
            *map(actors.spawn, itertools.repeat(Counter, 5))
        )
        print(await asyncio.gather(*(counter.add(1) for counter in counters)))
    async with pybeam.spawn(Counter, value=10) as counter:
        print(await counter.add(2))


if __name__ == "__main__":
    pybeam.run(main)
```

## Run an actor

Save this [example](../../examples/actors/main.py) as `main.py` and run it with Python. It prints `1`, then `[1, 1, 1, 1, 1]`, then `12`.

`@pybeam.actor` declares the state fields and typed async methods. `actors.spawn(Counter, value=0)` constructs one actor and returns its reference. `await first.add(1)` calls that actor's method and returns the new value.

The five counters have independent state. Their calls can be concurrent, but each counter handles its own calls serially. Exiting the group closes its actors. `pybeam.spawn(Counter, value=10)` is the shorter context manager for owning one actor.

`pybeam.run(main)` owns runtime startup and shutdown. Pass the async function, not `main()`. The file is loaded again inside the VM, so objects and closures from the launching interpreter are not transferred.
