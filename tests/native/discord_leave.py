import asyncio
from itertools import permutations

import pybeam
from examples.discord_simulation import Delivery, Event, Membership, Receipt, Room
from pybeam.actors import ActorCallError, ActorPayloadError, ActorStoppedError, context_counts


async def membership_lifecycle() -> None:
    from erlang.actors import ActorGenerationChanged

    baseline = await context_counts()
    async with pybeam.ActorGroup(max_children=1, max_pending=4) as group:
        room = await group.spawn(
            Room, room_id="leave", max_members=2, max_events=3, history_limit=3
        )
        assert await room.join("a", epoch=7) == 1
        assert await room.join("b", epoch=7) == 2
        events = [
            Event(room_id="leave", event_id=str(i), epoch=7, text="", tags=[]) for i in range(1, 4)
        ]
        first = await room.publish(events[0])
        assert first == Receipt("leave", "1", 7, (Delivery("a", 1), Delivery("b", 1)))
        assert await room.leave("b", epoch=7) == 1
        snapshot = await room.snapshot()
        assert snapshot.members == (Delivery("a", 1),)
        assert snapshot.ledger == (Membership("a", 7, 1, True), Membership("b", 7, 1, False))
        assert snapshot.history == (events[0],) and snapshot.event_ids == ("1",)
        assert await room.leave("b", epoch=7) == 1
        for member, epoch in (("missing", 7), ("b", 6), ("b", 8)):
            try:
                await room.leave(member, epoch=epoch)
            except ActorCallError as error:
                assert error.kind == "ValueError"
            else:
                raise AssertionError("Unknown or stale membership leave succeeded")
            assert await room.snapshot() == snapshot
        try:
            await room.leave("a", epoch=True)
        except ActorPayloadError:
            pass
        else:
            raise AssertionError("Boolean epoch bypassed the native argument codec")
        assert await room.snapshot() == snapshot
        try:
            await room.leave("a", epoch=7, revision=True)
        except ActorPayloadError:
            pass
        else:
            raise AssertionError("Boolean revision bypassed the native argument codec")
        assert await room.snapshot() == snapshot
        for member, epoch in (("", 7), ("x" * 33, 7), ("a", -1), ("a", 9_223_372_036_854_775_808)):
            try:
                await room.leave(member, epoch=epoch)
            except ActorCallError as error:
                assert error.kind == "ValueError"
            else:
                raise AssertionError("Invalid membership input changed native state")
            assert await room.snapshot() == snapshot
        second = await room.publish(events[1])
        assert second == Receipt("leave", "2", 7, (Delivery("a", 2),))
        try:
            await room.join("b", epoch=8)
        except ActorCallError:
            pass
        else:
            raise AssertionError("Rejoin changed the existing room epoch")
        assert await room.join("b", epoch=7) == 2
        before_stale = await room.snapshot()
        try:
            await room.leave("b", epoch=7)
        except ActorCallError:
            pass
        else:
            raise AssertionError("Old leave removed new membership")
        assert await room.snapshot() == before_stale
        third = await room.publish(events[2])
        assert third == Receipt("leave", "3", 7, (Delivery("a", 3), Delivery("b", 2)))
        final = await room.snapshot()
        assert final.epoch == 7
        assert final.members == (Delivery("a", 3), Delivery("b", 2))
        assert final.ledger == (Membership("a", 7, 3, True), Membership("b", 7, 2, True, 1))
        assert final.event_ids == ("1", "2", "3") and final.history == tuple(events)
        assert sum(member.count for member in final.ledger) == 5
        assert await room.leave("a", epoch=7) == 1
        assert await room.leave("b", epoch=7, revision=1) == 0
        departed = await room.snapshot()
        assert departed.members == ()
        assert sum(member.count for member in departed.ledger) == 5
        try:
            await room.join("a", epoch=8)
        except ActorCallError:
            pass
        else:
            raise AssertionError("Empty presence reset the existing room epoch")
        assert await room.snapshot() == departed
        try:
            await room.join("third-identity", epoch=7)
        except ActorCallError:
            pass
        else:
            raise AssertionError("Departed identity tombstones lost their capacity bound")
        assert await room.snapshot() == departed
        await group.crash(room)
        try:
            await room.leave("b", epoch=7, revision=1)
        except ActorGenerationChanged:
            pass
        else:
            raise AssertionError("Old native generation accepted leave")
        fresh = await group.rejoin(room)
        empty = await fresh.snapshot()
        assert empty.members == empty.event_ids == empty.history == empty.ledger == ()
        assert await fresh.join("b", epoch=8) == 1
        fresh_state = await fresh.snapshot()
        try:
            await fresh.leave("b", epoch=7)
        except ActorCallError:
            pass
        else:
            raise AssertionError("Old room epoch removed a new-generation membership")
        assert await fresh.snapshot() == fresh_state
        try:
            await room.leave("b", epoch=7, revision=1)
        except ActorStoppedError:
            pass
        else:
            raise AssertionError("Old reference removed fresh native membership")
        assert (await fresh.snapshot()).members == (Delivery("b", 0),)
    try:
        await fresh.leave("b", epoch=8)
    except ActorStoppedError:
        pass
    else:
        raise AssertionError("Leave after owner close executed")
    assert baseline.live <= (await context_counts()).live <= baseline.live + 1


async def racing_clients() -> None:
    baseline = await context_counts()
    # Every order is legal at the native serialization boundary. The observed
    # results plus authoritative ledger must match one complete serial order.
    expected: list[tuple[int, int | str, Receipt, tuple[Membership, ...]]] = []
    for order in permutations(("leave", "join", "publish")):
        present = True
        revision = 0
        b_count = 0
        joined: int | str = "ValueError"
        left = -1
        receipt = Receipt("race", "client-event", 0, ())
        for action in order:
            match action:
                case "leave":
                    if revision != 0:
                        raise AssertionError("Invalid oracle ordering")
                    present = False
                    left = 1
                case "join":
                    if not present:
                        present = True
                        revision = 1
                        joined = 2
                case "publish":
                    b_count = int(present)
                    receipt = Receipt(
                        "race",
                        "client-event",
                        0,
                        (Delivery("a", 1), Delivery("b", 1)) if present else (Delivery("a", 1),),
                    )
        expected.append(
            (
                left,
                joined,
                receipt,
                (Membership("a", 0, 1, True), Membership("b", 0, b_count, present, revision)),
            )
        )

    async with pybeam.ActorGroup(max_children=1, max_pending=4) as group:
        for _attempt in range(8):
            room = await group.spawn(
                Room, room_id="race", max_members=2, max_events=1, history_limit=1
            )
            assert await room.join("a", epoch=0) == 1
            assert await room.join("b", epoch=0) == 2

            async def returning_join(reference: Room) -> int | str:
                try:
                    return await reference.join("b", epoch=0)
                except ActorCallError as error:
                    return error.kind

            async with asyncio.TaskGroup() as clients:
                left_task = clients.create_task(room.leave("b", epoch=0))
                joined_task = clients.create_task(returning_join(room))
                published = clients.create_task(
                    room.publish(
                        Event(room_id="race", event_id="client-event", epoch=0, text="", tags=[])
                    )
                )
            snapshot = await room.snapshot()
            assert (
                left_task.result(),
                joined_task.result(),
                published.result(),
                snapshot.ledger,
            ) in expected
            assert snapshot.members == tuple(
                Delivery(member.member_id, member.count)
                for member in snapshot.ledger
                if member.present
            )
            assert snapshot.event_ids == ("client-event",)
            assert tuple(event.event_id for event in snapshot.history) == ("client-event",)
            await group.stop(room)
    assert baseline.live <= (await context_counts()).live <= baseline.live + 1


async def main() -> None:
    async with asyncio.timeout(25):
        await membership_lifecycle()
        await racing_clients()
    print("discord-leave-native-ok")


if __name__ == "__main__":
    pybeam.run(main)
