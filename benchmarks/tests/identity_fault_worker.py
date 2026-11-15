"""Embedded native test worker. Corrupt delivery to the real Room handler."""

import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pybeam import run
from pybeam.actors import ActorRef

from benchmarks import pybeam_reference_worker as worker
from benchmarks.contract import Applied, Message
from benchmarks.tests.identity_fault import Fault, RecordedReplies, changed_id


async def main() -> None:
    directory = Path(sys.argv[1])
    fault = Fault.model_validate_json((directory / "identity-fault.json").read_text())
    original_produce = worker.produce
    original_add = worker.Room.add
    active = False
    replies: list[Applied] = []

    async def injected_add(self: worker.Room, message_id: int, increment: int) -> Applied:
        delivered_id = (
            changed_id(fault.mode, message_id)
            if active and fault.mode in {"duplicate_id", "substitute_id"}
            else message_id
        )
        reply = await original_add(self, delivered_id, increment)
        if active:
            replies.append(reply)
        return replace(reply, count=reply.count + 1) if active and fault.mode == "count" else reply

    async def injected_produce(
        actors: Sequence[ActorRef[worker.Room]],
        sequence: tuple[Message, ...],
        limit: int,
    ) -> tuple[tuple[worker.Observation, ...], int]:
        nonlocal active
        active = len(sequence) == 100
        if active and fault.mode == "route":
            actors = [*actors[1:], actors[0]]
        return await original_produce(actors, sequence, limit)

    try:
        with (
            patch.object(worker.Room, "add", injected_add),
            patch.object(worker, "produce", injected_produce),
        ):
            await worker.main()
    finally:
        (directory / "received-replies.json").write_text(
            RecordedReplies(fault=fault, replies=tuple(replies)).model_dump_json()
        )


if __name__ == "__main__":
    run(main)
