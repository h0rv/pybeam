import asyncio
from threading import Barrier, get_ident

import pybeam
from pybeam.actors import context_counts

PEERS = Barrier(4)


def wait_for_peers() -> int:
    PEERS.wait(timeout=5)
    return get_ident()


@pybeam.actor
class Blocking:
    async def meet(self) -> int:
        return wait_for_peers()


async def verify() -> None:
    async with pybeam.ActorGroup() as left, pybeam.ActorGroup() as right:
        actors = [await group.spawn(Blocking) for group in (left, right) for _ in range(2)]
        async with asyncio.TaskGroup() as calls:
            pending = [calls.create_task(actor.meet()) for actor in actors]
        assert len({call.result() for call in pending}) == 4
        warm = await context_counts()
        async with asyncio.TaskGroup() as calls:
            pending = [calls.create_task(actor.meet()) for actor in actors]
        assert len({call.result() for call in pending}) == 4
        assert await context_counts() == warm
    assert await context_counts() == warm
    print("actor-worker-pool-ok", flush=True)


if __name__ == "__main__":
    pybeam.run(verify)
