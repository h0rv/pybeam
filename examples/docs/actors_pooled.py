from actors import Counter

import pybeam
from pybeam import ActorGroup, ExecutionLimits


async def pooled() -> None:
    async with ActorGroup(
        max_children=800,
        max_pending=8,
        max_bytes=65_536,
        execution=ExecutionLimits(workers=4, capacity=32, max_bytes=1_048_576),
    ) as actors:
        counter = await actors.spawn(Counter)
        await counter.add(1)


if __name__ == "__main__":
    pybeam.run(pooled)
