import asyncio
import itertools
from dataclasses import field
from typing import assert_type

import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, n: int, *, label: str = "") -> int:
        self.value += n
        return self.value


@pybeam.actor
class Derived(Counter):
    step: int = 1

    async def advance(self) -> int:
        return await self.add(self.step)


@pybeam.actor
class Required:
    value: int
    private: list[int] = field(init=False, default_factory=list)
    label: str = field(kw_only=True, default="")

    async def read(self) -> int:
        return self.value


async def positive() -> None:
    async with pybeam.ActorGroup() as actors:
        required = await actors.spawn(Required, value=1, label="ok")
        assert_type(required, Required)
        assert_type(await required.read(), int)
        first = await actors.spawn(Counter, value=0)
        assert_type(first, Counter)
        assert_type(await first.add(1, label="one"), int)
        rejoined = await actors.rejoin(first)
        assert_type(rejoined, Counter)
        assert_type(await rejoined.add(1), int)
        second = await actors.spawn(Derived, value=1, step=2)
        assert_type(second, Derived)
        assert_type(await second.advance(), int)
        assert_type(await second.add(1), int)
        counters = await asyncio.gather(*(actors.spawn(Counter) for _ in range(5)))
        assert_type(counters, list[Counter])
        mapped: list[Counter] = await asyncio.gather(
            *map(actors.spawn, itertools.repeat(Counter, 5))
        )
        assert_type(mapped, list[Counter])
        for counter in mapped:
            assert_type(await counter.add(1), int)
    async with pybeam.spawn(Counter, value=2) as counter:
        assert_type(counter, Counter)
        assert_type(await counter.add(2), int)
