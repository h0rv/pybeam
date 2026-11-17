import asyncio
from dataclasses import field

import pytest

from pybeam import ActorGroup, actor, write_state
from pybeam.actors import ActorCallError, ActorConstructionError, ActorStateAccessError
from pybeam.actors import groups, state
from tests.test_pythonic_actors import ScriptedGroup


@actor
class Progress:
    step: int = 0
    history: list[int] = field(default_factory=list)
    private: bytes = field(init=False, default=b"initial")

    async def advance(self, step: int, *, save: bool = False) -> int:
        self.step = step
        self.history.append(step)
        self.private = b"changed"
        return await write_state(self) if save else 0

    async def read(self) -> tuple[int, list[int], bytes]:
        return self.step, self.history, self.private

    async def child_write(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(write_state(self))


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[bytes, tuple[int, bytes]] = {}
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False

    async def async_call(self, name: str, *args: object) -> object:
        if name == "_actor_state_open":
            return "ok"
        key = args[0]
        assert isinstance(key, bytes)
        if name == "_actor_state_read":
            if key not in self.records:
                return "missing"
            version, payload = self.records[key]
            return "ok", version, payload
        assert name == "_actor_state_write"
        self.entered.set()
        await self.release.wait()
        if self.fail:
            return "error", "disk_unavailable"
        expected, payload = args[1:]
        assert type(expected) is int and isinstance(payload, bytes)
        current, _ = self.records.get(key, (0, b""))
        if expected != current:
            return "error", ("version_conflict", expected, current)
        self.records[key] = current + 1, payload
        return "ok", current + 1


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    native = ScriptedGroup()
    storage = MemoryStore()

    async def open_group(*, max_children: int) -> ScriptedGroup:
        return native

    monkeypatch.setattr(groups, "open_group", open_group)
    monkeypatch.setattr(state, "erlang", storage)
    return storage


async def test_explicit_writes_restore_all_fields_without_saving_dirty_changes(
    store: MemoryStore,
) -> None:
    async with ActorGroup() as group:
        progress = await group.restore("job", Progress)
        assert await progress.advance(2, save=True) == 1
        assert await progress.advance(3, save=True) == 2
        await progress.advance(99)
        restored = await group.restore("job", Progress, 800)
        assert await restored.read() == (3, [2, 3], b"changed")
        assert await restored.advance(4, save=True) == 3
        other = await group.restore("other-job", Progress)
        assert await other.read() == (0, [], b"initial")


async def test_stale_versions_do_not_overwrite_and_require_restart(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        first = await group.restore("job", Progress)
        stale = await group.restore("job", Progress)
        assert await first.advance(1, save=True) == 1
        with pytest.raises(ActorCallError) as failure:
            await stale.advance(2, save=True)
        assert failure.value.kind == "ActorStateConflictError"
        with pytest.raises(ActorCallError, match="restart"):
            await stale.advance(3, save=True)
        assert next(iter(store.records.values()))[0] == 1


async def test_write_ownership_and_ephemeral_actors(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        ephemeral = await group.spawn(Progress)
        with pytest.raises(ActorCallError) as failure:
            await ephemeral.advance(1, save=True)
        assert failure.value.kind == "ActorStateAccessError"
        with pytest.raises(ActorStateAccessError):
            await write_state(ephemeral)
        persistent = await group.restore("job", Progress)
        with pytest.raises(ActorCallError, match="ActorStateAccessError"):
            await persistent.child_write()
        assert not store.records


async def test_cancelled_write_settles_version_before_propagating(store: MemoryStore) -> None:
    spec = state.SnapshotSpec.build(Progress, "job", groups.Path.cwd(), 4096)
    snapshot = state.Snapshot(spec)
    store.release.clear()
    pending = asyncio.create_task(snapshot.write(Progress()))
    await store.entered.wait()
    pending.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert snapshot.version == 1
    assert await snapshot.write(Progress()) == 2


async def test_storage_failure_prevents_blind_retry(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        progress = await group.restore("job", Progress)
        store.fail = True
        with pytest.raises(ActorCallError, match="disk_unavailable"):
            await progress.advance(1, save=True)
        store.fail = False
        with pytest.raises(ActorCallError, match="restart"):
            await progress.advance(2, save=True)


async def test_incompatible_snapshot_fails_start_without_replacing_saved_data(
    store: MemoryStore,
) -> None:
    async with ActorGroup() as group:
        progress = await group.restore("job", Progress)
        await progress.advance(1, save=True)
        key = next(iter(store.records))
        store.records[key] = 1, b'{"step":"invalid","history":[],"private":""}'
        with pytest.raises(ActorConstructionError):
            await group.restore("job", Progress)
        assert store.records[key][0] == 1


async def test_snapshot_limit_and_empty_key_fail_without_writing(store: MemoryStore) -> None:
    async with ActorGroup(max_bytes=80) as group:
        with pytest.raises(ValueError, match="nonempty"):
            await group.restore("", Progress)
        progress = await group.restore("job", Progress)
        for step in range(30):
            await progress.advance(step)
        with pytest.raises(ActorCallError, match="max_bytes"):
            await progress.advance(31, save=True)
        assert not store.records
