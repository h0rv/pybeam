import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import ConnectionPoolEntry, QueuePool

import pybeam
from examples.sqlalchemy.store import Command, Database, Mode, Note, Notebook, add_note, databases
from pybeam.actors.codec import ActorCallError
from pybeam.codec import Json
from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor

entered, release = Event(), Event()


async def hold_async(db: Database) -> None:
    async with db.async_sessions.begin() as session:
        session.add(Note(text="crash-rollback"))
        await session.flush()
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        raise ValueError("rollback after crash")


@pybeam.actor
class HeldNotebook:
    async def hold(self, mode: Mode) -> None:
        db = databases["notes"]
        if mode == "async":
            await db.on_owner(hold_async(db))
        else:
            with db.sync_sessions.begin() as session:
                session.add(Note(text="crash-rollback"))
                session.flush()
                entered.set()
                assert await asyncio.to_thread(release.wait, 5)
                raise ValueError("rollback after crash")


async def unrouted(value: int) -> int:
    async with databases["notes"].asynchronous.connect():
        return value


async def loop_affinity(db: Database) -> None:
    async with db.asynchronous.connect(), db.asynchronous.connect():
        waiting = asyncio.create_task(unrouted(0))
        await asyncio.sleep(0)
        try:
            async with TaskSupervisor(
                TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=5000)
            ) as tasks:
                ref = await tasks.start(
                    TaskFunction(unrouted, Json(int), Json(int), dedicated_loop=True), 1
                )
                error = await ref.exception()
                assert error is not None
                assert "bound to a different event loop" in str(error)
        finally:
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting
    print("sqlalchemy-constraint-ok: an unrouted pooled async engine rejects a foreign loop")


async def crash_checked_out(db: Database, mode: Mode) -> None:
    entered.clear()
    release.clear()
    async with pybeam.ActorGroup() as actors:
        notebook = await actors.spawn(HeldNotebook)
        call = asyncio.create_task(notebook.hold(mode))
        assert await asyncio.to_thread(entered.wait, 5)
        engine = db.sync if mode == "sync" else db.asynchronous.sync_engine
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.checkedout() == 1
        crash = asyncio.create_task(actors.crash(notebook))
        await asyncio.sleep(0.05)
        assert not crash.done()
        release.set()
        await crash
        try:
            await call
        except RuntimeError:
            pass
        else:
            raise AssertionError("The failed actor call succeeded")
        await actors.rejoin(notebook)
    assert engine.pool.checkedout() == 0
    assert "crash-rollback" not in await db.read(mode)


@dataclass
class Connections:
    opened: int = 0
    closed: int = 0
    checkouts: int = 0
    lock: Lock = field(default_factory=Lock)

    def connect(self, connection: object, record: ConnectionPoolEntry) -> None:
        with self.lock:
            self.opened += 1

    def close(self, connection: object, record: ConnectionPoolEntry) -> None:
        with self.lock:
            self.closed += 1

    def checkout(self, connection: object, record: ConnectionPoolEntry, proxy: object) -> None:
        with self.lock:
            self.checkouts += 1

    def watch(self, engine: Engine) -> None:
        event.listen(engine, "connect", self.connect)
        event.listen(engine, "close", self.close)
        event.listen(engine, "checkout", self.checkout)


async def exercise(db: Database, mode: Mode) -> None:
    assert await db.read(mode) == []
    async with pybeam.ActorGroup() as actors:
        notebooks = [await actors.spawn(Notebook) for _ in range(4)]
        for turn in range(2):
            async with asyncio.TaskGroup() as group:
                for i, notebook in enumerate(notebooks):
                    group.create_task(notebook.add(Command(mode, "actor-{}-{}".format(turn, i))))
        try:
            await notebooks[0].add(Command(mode, "rollback", fail=True))
        except ActorCallError as error:
            assert "rollback requested" in str(error)
        else:
            raise AssertionError("Actor committed a failed transaction")
        before = await notebooks[0].read(mode)
        assert len(before) == 8
        await actors.crash(notebooks[0])
        replacement = await actors.rejoin(notebooks[0])
        assert await replacement.read(mode) == before
        assert await replacement.add(Command(mode, "restarted")) == "restarted"
    for dedicated in (False, True):
        function = TaskFunction(add_note, Json(Command), Json(str), dedicated_loop=dedicated)
        async with TaskSupervisor(
            TaskLimits(capacity=4, max_bytes=4096, shutdown_ms=5000)
        ) as supervisor:
            for turn in range(2):
                refs = [
                    await supervisor.start(
                        function, Command(mode, "task-{}-{}-{}".format(dedicated, turn, i))
                    )
                    for i in range(4)
                ]
                assert len([await ref for ref in refs]) == 4
            failed = await supervisor.start(function, Command(mode, "task-rollback", fail=True))
            assert await failed.exception() is not None
    notes = await db.read(mode)
    assert len(notes) == 25
    assert "rollback" not in notes
    assert "task-rollback" not in notes


async def verify() -> None:
    db = Database(Path.cwd())
    sync, asynchronous = Connections(), Connections()
    sync.watch(db.sync)
    asynchronous.watch(db.asynchronous.sync_engine)
    try:
        await db.start()
        databases["notes"] = db
        await exercise(db, "sync")
        await exercise(db, "async")
        await crash_checked_out(db, "sync")
        await crash_checked_out(db, "async")
        await loop_affinity(db)
        for engine, counters in ((db.sync, sync), (db.asynchronous.sync_engine, asynchronous)):
            assert isinstance(engine.pool, QueuePool)
            assert engine.pool.checkedout() == 0
            assert 1 <= counters.opened <= 2
            assert counters.closed == 0
            assert counters.checkouts > counters.opened * 10
    finally:
        databases.clear()
        await db.close()
    assert sync.closed == sync.opened
    assert asynchronous.closed == asynchronous.opened
    print("sqlalchemy-pools-ok: sync/async actors, restart, shared/dedicated tasks, dispose")


if __name__ == "__main__":
    pybeam.run(verify)
