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
