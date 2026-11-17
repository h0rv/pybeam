import asyncio
from contextlib import suppress

import pybeam
from examples.discord_simulation import Config, Event, Room, Snapshot, simulate
from pybeam.actors import ActorCallError, ActorStoppedError, context_counts


async def rejected(room: Room, event: Event) -> None:
    try:
        await room.publish(event)
    except ActorCallError as error:
        assert error.kind == "ValueError"
    else:
        raise AssertionError("Invalid event executed")


async def rooms_and_recovery() -> None:
    from erlang.actors import ActorGenerationChanged

    baseline = await context_counts()
    async with pybeam.ActorGroup(max_children=2, max_pending=2, max_bytes=8192) as group:
        first = await group.spawn(
            Room, room_id="first", max_members=2, max_events=2, history_limit=1
        )
        second = await group.spawn(
            Room, room_id="second", max_members=1, max_events=1, history_limit=1
        )
        assert await first.join("a", epoch=0) == 1
        assert await first.join("b", epoch=0) == 2
        assert await second.join("c", epoch=0) == 1
        for member in ("a", "overflow"):
            try:
                await first.join(member, epoch=0)
            except ActorCallError as error:
                assert error.kind == "ValueError"
            else:
                raise AssertionError("Duplicate or over-capacity membership was accepted")
        event = Event(room_id="first", event_id="0:0", epoch=0, text="hello", tags=["original"])
        receipt = await first.publish(event)
        assert (receipt.room_id, receipt.event_id, receipt.epoch) == ("first", "0:0", 0)
        assert tuple((item.member_id, item.count) for item in receipt.deliveries) == (
            ("a", 1),
            ("b", 1),
        )
        event.tags.append("caller mutation")
        snapshot = await first.snapshot()
        assert snapshot.history[0].tags == ["original"]
        snapshot.history[0].tags.append("reply mutation")
        assert (await first.snapshot()).history[0].tags == ["original"]
        await rejected(second, event)
        await rejected(first, event)
        await rejected(first, Event(room_id="first", event_id="old", epoch=1, text="", tags=[]))
        assert (await first.snapshot()).event_ids == ("0:0",)
        assert (await second.snapshot()).event_ids == ()
        await first.publish(Event(room_id="first", event_id="0:1", epoch=0, text="", tags=[]))
        assert tuple(e.event_id for e in (await first.snapshot()).history) == ("0:1",)
        await rejected(first, Event(room_id="first", event_id="0:2", epoch=0, text="", tags=[]))
        before = await group.info(first)
        other_before = await group.info(second)
        await group.crash(first)
        for control in (group.crash, group.info):
            try:
                await control(first)
            except ActorGenerationChanged as error:
                assert error.expected == before.generation
                assert error.current != before.generation
            else:
                raise AssertionError("Stale control touched the replacement")
        try:
            await first.publish(event)
        except ActorGenerationChanged:
            pass
        else:
            raise AssertionError("Failed send was automatically replayed")
        fresh = await group.rejoin(first)
        after = await group.info(fresh)
        assert before.pid != after.pid
        assert before.generation != after.generation
        assert await fresh.snapshot() == Snapshot("first", 0, (), (), ())
        await rejected(fresh, Event(room_id="first", event_id="1:0", epoch=0, text="", tags=[]))
        assert await fresh.join("a", epoch=1) == 1
        recovered = await fresh.publish(
            Event(room_id="first", event_id="1:0", epoch=1, text="", tags=[])
        )
        assert recovered.room_id == "first" and recovered.event_id == "1:0"
        assert tuple((item.member_id, item.count) for item in recovered.deliveries) == (("a", 1),)
        assert (await fresh.snapshot()).event_ids == ("1:0",)
        assert await group.info(second) == other_before
    try:
        await fresh.snapshot()
    except ActorStoppedError:
        pass
    else:
        raise AssertionError("Closed owner reference survived")
    assert baseline.live <= (await context_counts()).live <= baseline.live + 2


@pybeam.actor
class Waiting:
    async def wait(self) -> None:
        await asyncio.sleep(0.2)


async def owner_cancellation() -> None:
    baseline = await context_counts()
    started = asyncio.Event()

    async def owner() -> None:
        async with pybeam.ActorGroup(max_children=2) as group:
            refs = [await group.spawn(Waiting) for _ in range(2)]
            async with asyncio.TaskGroup() as calls:
                for ref in refs:
                    calls.create_task(ref.wait())
                async with asyncio.timeout(5):
                    for ref in refs:
                        while (await group.info(ref)).admitted_count == 0:
                            pass
                started.set()
                await asyncio.Event().wait()

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(owner())
        await started.wait()
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
    assert baseline.live <= (await context_counts()).live <= baseline.live + 2


async def main() -> None:
    async with asyncio.timeout(25):
        proof = await simulate(Config(rooms=2, members=3, events=6, max_inflight=2))
        assert proof.fanout_receipts_before_fault == 18
        assert proof.fanout_receipts_after_rejoin == 3
        await rooms_and_recovery()
        await owner_cancellation()
    print("discord-native-ok")


if __name__ == "__main__":
    pybeam.run(main)
