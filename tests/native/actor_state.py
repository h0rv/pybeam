from pybeam import ActorGroup
from tests.fixtures.persistent_actor import Progress


async def actor_restart() -> bool:
    async with ActorGroup() as group:
        progress = await group.restore("native-restart", Progress)
        assert await progress.advance(7, save=True) == 1
        await progress.advance(99)
        await group.crash(progress)
        progress = await group.rejoin(progress)
        assert await progress.read() == (7, [7], b"changed")
        assert await progress.advance(8, save=True) == 2
    async with ActorGroup() as group:
        progress = await group.restore("native-restart", Progress, 123)
        assert await progress.read() == (8, [7, 8], b"changed")
    return True


async def prepare_crash() -> bool:
    async with ActorGroup() as group:
        progress = await group.restore("vm-crash", Progress)
        assert await progress.advance(42, save=True) == 1
        await progress.advance(99)
    return True


async def verify_crash() -> bool:
    async with ActorGroup() as group:
        progress = await group.restore("vm-crash", Progress, 1000)
        assert await progress.read() == (42, [42], b"changed")
        assert await progress.advance(43, save=True) == 2
    return True
