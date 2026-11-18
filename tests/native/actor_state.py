import asyncio

from pybeam import ActorGroup, actor, write_state
from pybeam.actors import ActorStateError
from tests.fixtures.persistent_actor import Progress

try:
    import erlang
except ModuleNotFoundError as error:
    if error.name != "erlang":
        raise
    erlang = None


@actor
class Counter:
    count: int = 0

    async def save(self, count: int) -> int:
        self.count = count
        return await write_state(self)

    async def read(self) -> int:
        return self.count


_shutdown = asyncio.Event()
_stopped = asyncio.Event()


async def _own(group: ActorGroup) -> None:
    if erlang is None:
        raise RuntimeError("Requires the managed runtime")
    counter = await group.restore("k", Counter)
    count = await counter.read()
    await erlang.async_call("actor_owned", count)
    if count == 0:
        assert await counter.save(5) == 1
        await erlang.async_call("actor_saved", 5)
    await _shutdown.wait()


async def cluster_run(nodes: list[str]) -> bool:
    try:
        async with ActorGroup(replicas=nodes) as group, asyncio.TaskGroup() as tasks:
            owner = tasks.create_task(_own(group))
            await _shutdown.wait()
            owner.cancel()
    finally:
        _stopped.set()
    return True


async def cluster_stop() -> bool:
    _shutdown.set()
    await _stopped.wait()
    return True


async def replica_validation() -> bool:
    async with ActorGroup(replicas=["a@host", "b@host"]) as group:
        try:
            await group.restore("k", Counter)
        except ActorStateError as error:
            assert "at_least_three_replicas_required" in str(error)
        else:
            raise AssertionError("Two replicas were accepted")
    return True


async def ownership_lifecycle() -> bool:
    async with ActorGroup() as group, asyncio.TaskGroup() as tasks:
        first = await group.restore("local-owner", Counter)
        standby = tasks.create_task(group.restore("local-owner", Counter))
        try:
            async with asyncio.timeout(0.2):
                await asyncio.shield(standby)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Standby acquired an owned key")
        assert await first.save(5) == 1
        await group.crash(first)
        first = await group.rejoin(first)
        assert await first.read() == 5
        assert not standby.done()
        await group.stop(first)
        async with asyncio.timeout(10):
            second = await standby
        assert await second.read() == 5
        assert await second.save(6) == 2
    return True


async def cancel_standby() -> bool:
    async with ActorGroup() as group, asyncio.TaskGroup() as tasks:
        first = await group.restore("cancelled-standby", Counter)
        standby = tasks.create_task(group.restore("cancelled-standby", Counter))
        try:
            async with asyncio.timeout(0.2):
                await asyncio.shield(standby)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Standby acquired an owned key")
        standby.cancel()
        try:
            await standby
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Standby ignored cancellation")
        assert await first.save(5) == 1
        await group.stop(first)
        async with asyncio.timeout(10):
            fresh = await group.restore("cancelled-standby", Counter)
        assert await fresh.read() == 5
        assert await fresh.save(6) == 2
    return True


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
        print("actor-state-ready", flush=True)
        await asyncio.Event().wait()
    return True


async def verify_crash() -> bool:
    async with ActorGroup() as group:
        progress = await group.restore("vm-crash", Progress, 1000)
        assert await progress.read() == (42, [42], b"changed")
        assert await progress.advance(43, save=True) == 2
    return True
