import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from pybeam import ActorGroup, write_state
from pybeam.actors import (
    ActorCallError,
    ActorConstructionError,
    ActorStateAccessError,
    groups,
    state,
)
from tests.fixtures.persistent_actor import Progress
from tests.test_pythonic_actors import ScriptedGroup


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[bytes, tuple[int, bytes]] = {}
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False
        self.lose_ack = False
        self.owners: dict[bytes, bytes] = {}
        self.refs: dict[bytes, bytes] = {}
        self.cancelled: set[bytes] = set()
        self.changed = asyncio.Condition()
        self.claims: asyncio.Queue[bytes] = asyncio.Queue()
        self.claim_ack = asyncio.Event()
        self.claim_ack.set()
        self.claimed = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.opens: list[tuple[object, ...]] = []
        self.open_error: object = "ok"

    async def async_call(self, name: str, *args: object) -> object:
        if name == "_actor_state_open":
            self.opens.append(args)
            return self.open_error
        key = args[0]
        assert isinstance(key, bytes)
        if name == "_actor_state_claim":
            ref = args[1]
            assert isinstance(ref, bytes)
            self.claims.put_nowait(ref)
            async with self.changed:
                await self.changed.wait_for(lambda: key not in self.owners or ref in self.cancelled)
                if ref in self.cancelled:
                    return "error", "cancelled"
                self.owners[key] = ref
                self.refs[ref] = key
                self.claimed.set()
            await self.claim_ack.wait()
            return "ok", ref
        if name in {"_actor_state_cancel", "_actor_state_release"}:
            async with self.changed:
                if name == "_actor_state_cancel":
                    self.cancelled.add(key)
                    self.cancel_seen.set()
                owned = self.refs.pop(key, None)
                if owned is not None and self.owners.get(owned) == key:
                    del self.owners[owned]
                self.changed.notify_all()
            return "ok"
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
        expected, payload, token = args[1:]
        if self.owners.get(key) != token:
            return "error", "not_owner"
        assert type(expected) is int and isinstance(payload, bytes)
        current, _ = self.records.get(key, (0, b""))
        if expected != current:
            return "error", ("version_conflict", expected, current)
        self.records[key] = current + 1, payload
        if self.lose_ack:
            raise ConnectionError("reply lost after commit")
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
        await group.stop(progress)
        restored = await group.restore("job", Progress, 800)
        assert await restored.read() == (3, [2, 3], b"changed")
        assert await restored.advance(4, save=True) == 3
        other = await group.restore("other-job", Progress)
        assert await other.read() == (0, [], b"initial")


async def test_second_restore_waits_until_stop_then_reads_committed_state(
    store: MemoryStore,
) -> None:
    async with ActorGroup() as group:
        first = await group.restore("job", Progress)
        await store.claims.get()
        pending = asyncio.create_task(group.restore("job", Progress))
        await store.claims.get()
        assert not pending.done()
        assert await first.advance(1, save=True) == 1
        await group.stop(first)
        second = await pending
        assert await second.read() == (1, [1], b"changed")
        assert await second.advance(2, save=True) == 2
    assert not store.owners


async def test_write_ownership_and_ephemeral_actors(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        ephemeral = await group.spawn(Progress)
        with pytest.raises(ActorCallError) as failure:
            await ephemeral.advance(1, save=True)
        assert failure.value.kind == "ActorStateAccessError"
        with pytest.raises(ActorStateAccessError):
            await write_state(ephemeral)
        persistent = await group.restore("job", Progress)
        with pytest.raises(ActorCallError, match="ExceptionGroup"):
            await persistent.child_write()
        assert not store.records


async def test_cancelled_write_settles_version_before_propagating(store: MemoryStore) -> None:
    spec = state.SnapshotSpec.build(Progress, "job", Path.cwd(), 4096)
    snapshot = state.Snapshot(await state.ClaimedSnapshot.acquire(spec, ()))
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


async def test_lost_ack_requires_restart_and_restores_the_committed_snapshot(
    store: MemoryStore,
) -> None:
    async with ActorGroup() as group:
        progress = await group.restore("job", Progress)
        store.lose_ack = True
        with pytest.raises(ActorCallError, match="Actor state operation failed"):
            await progress.advance(42, save=True)
        store.lose_ack = False
        with pytest.raises(ActorCallError, match="restart"):
            await progress.advance(99, save=True)
        await group.stop(progress)
        restored = await group.restore("job", Progress)
        assert await restored.read() == (42, [42], b"changed")
        assert await restored.advance(43, save=True) == 2


async def test_incompatible_snapshot_fails_start_without_replacing_saved_data(
    store: MemoryStore,
) -> None:
    async with ActorGroup() as group:
        progress = await group.restore("job", Progress)
        await progress.advance(1, save=True)
        key = next(iter(store.records))
        store.records[key] = 1, b'{"step":"invalid","history":[],"private":""}'
        await group.stop(progress)
        with pytest.raises(ActorConstructionError):
            await group.restore("job", Progress)
        assert store.records[key][0] == 1
        assert not store.owners


async def test_snapshot_limit_and_empty_key_fail_without_writing(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        with pytest.raises(ValueError, match="nonempty"):
            await group.restore("", Progress)
        spec = state.SnapshotSpec.build(Progress, "job", Path.cwd(), 80)
        snapshot = state.Snapshot(await state.ClaimedSnapshot.acquire(spec, ()))
        with pytest.raises(state.ActorStateError, match="max_bytes"):
            await snapshot.write(Progress(history=list(range(30))))
        assert not store.records


async def test_cancel_waiting_restore_does_not_leak_ownership(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        first = await group.restore("job", Progress)
        await store.claims.get()
        pending = asyncio.create_task(group.restore("job", Progress))
        ref = await store.claims.get()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert ref in store.cancelled
        await group.stop(first)
        restored = await group.restore("job", Progress)
        assert await restored.read() == (0, [], b"initial")
    assert not store.owners


async def test_cancel_claim_that_won_before_ack_releases_it(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        store.claim_ack.clear()
        pending = asyncio.create_task(group.restore("job", Progress))
        await store.claimed.wait()
        pending.cancel()
        await store.cancel_seen.wait()
        pending.cancel()
        store.claim_ack.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not store.owners
        restored = await group.restore("job", Progress)
        assert await restored.advance(5, save=True) == 1


async def test_group_exit_cancels_standby_and_releases_owner(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        await group.restore("job", Progress)
        await store.claims.get()
        pending = asyncio.create_task(group.restore("job", Progress))
        await store.claims.get()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not store.owners


async def test_fenced_actor_reports_ownership_loss(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        progress = await group.restore("job", Progress)
        store.owners.clear()
        with pytest.raises(ActorCallError) as failure:
            await progress.advance(1, save=True)
        assert failure.value.kind == "ActorStateOwnershipError"
        assert not store.records


async def test_version_conflict_remains_distinct(store: MemoryStore) -> None:
    async with ActorGroup() as group:
        progress = await group.restore("job", Progress)
        await progress.advance(1, save=True)
        key = next(iter(store.records))
        store.records[key] = 2, store.records[key][1]
        with pytest.raises(ActorCallError) as failure:
            await progress.advance(2, save=True)
        assert failure.value.kind == "ActorStateConflictError"
        with pytest.raises(ActorCallError, match="restart"):
            await progress.advance(3, save=True)


async def test_replica_names_are_forwarded_and_runtime_errors_surface(store: MemoryStore) -> None:
    nodes = ["a@host", "b@host", "c@host"]
    async with ActorGroup(replicas=nodes) as group:
        await group.restore("job", Progress)
        assert store.opens[-1][1] == [node.encode() for node in nodes]
    store.open_error = "error", "at_least_three_replicas_required"
    async with ActorGroup(replicas=nodes[:2]) as group:
        with pytest.raises(state.ActorStateError, match="at_least_three_replicas_required"):
            await group.restore("job", Progress)


@pytest.mark.parametrize("replicas", ["a@host", b"a@host", [""], ["a\x00@host"]])
def test_invalid_replica_names(replicas: Sequence[str]) -> None:
    with pytest.raises(ValueError, match="replicas"):
        ActorGroup(replicas=replicas)
