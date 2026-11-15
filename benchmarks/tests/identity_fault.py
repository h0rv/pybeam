"""Guardian-owned test adapter that corrupts actual delivery, not result files."""

import asyncio
import sys
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from benchmarks import pybeam_reference, python_reference
from benchmarks.contract import Applied, Message, StrictModel, Workload
from benchmarks.ownership import Identity
from benchmarks.pybeam_reference import Bootstrap
from benchmarks.python_reference import Observation, Peaks, Pending


class Fault(StrictModel):
    backend: Literal["python", "pybeam", "smoke"]
    mode: Literal["route", "duplicate_id", "substitute_id", "count"]
    full_delivery: bool = False


class RecordedReplies(StrictModel):
    fault: Fault
    replies: tuple[Applied, ...]


def changed_id(mode: str, message_id: int) -> int:
    return 0 if mode == "duplicate_id" else message_id + 100


def inject_python(directory: Path, fault: Fault) -> None:
    original_dispatch = python_reference.dispatch
    original_verify = python_reference.verify_applied
    active = False
    replies: list[Applied] = []

    def recorded_verify(message: Message, reply: Applied) -> None:
        if active:
            replies.append(reply)
        if not fault.full_delivery:
            original_verify(message, reply)

    async def rotated(
        sequence: tuple[Message, ...],
        queues: list[asyncio.Queue[Pending | None]],
        window: int,
        peaks: Peaks,
    ) -> tuple[Observation, ...]:
        nonlocal active
        active = len(sequence) == 100
        if active and fault.mode == "route":
            queues = queues[1:] + queues[:1]
        return await original_dispatch(sequence, queues, window, peaks)

    class ReplacedPending(Pending):
        def __init__(self, message: Message, applied: asyncio.Future[Applied]) -> None:
            # IDs are metadata in the delivered envelope. The real worker sees
            # this corrupted ID; invoke still holds the original request label.
            delivered = (
                message.model_copy(update={"id": changed_id(fault.mode, message.id)})
                if active and fault.mode != "route"
                else message
            )
            super().__init__(delivered, applied)

    sys.argv = [sys.argv[0], str(directory)]
    try:
        with (
            patch.object(python_reference, "verify_applied", recorded_verify),
            patch.object(python_reference, "dispatch", rotated),
            patch.object(python_reference, "Pending", ReplacedPending),
        ):
            python_reference.main()
    finally:
        (directory / "received-replies.json").write_text(
            RecordedReplies(fault=fault, replies=tuple(replies)).model_dump_json()
        )


def inject_smoke(directory: Path, fault: Fault) -> None:
    from benchmarks import python_smoke

    original_messages = python_smoke.messages

    # Smoke uses a nested producer. Change its delivered Message sequence while
    # retaining the full requested workload for the common guardian oracle.
    def delivered(workload: Workload, count: int) -> tuple[Message, ...]:
        sequence = original_messages(workload, count)
        if count != 100:
            return sequence
        if fault.mode == "route":
            return tuple(
                message.model_copy(update={"entity": (message.entity + 1) % workload.entities})
                for message in sequence
            )
        return tuple(
            message.model_copy(update={"id": changed_id(fault.mode, message.id)})
            for message in sequence
        )

    sys.argv = [sys.argv[0], str(directory)]
    with patch.object(python_smoke, "messages", delivered):
        python_smoke.main()


def inject_pybeam(directory: Path, fault: Fault) -> None:
    original_spawn = pybeam_reference.spawn_owned

    def injected_spawn(trial_directory: Path, argv: tuple[str, ...]) -> Identity:
        path = trial_directory / "bootstrap.json"
        bootstrap = Bootstrap.model_validate_json(path.read_text())
        worker = Path(__file__).with_name("identity_fault_worker.py")
        bootstrap = bootstrap.model_copy(update={"argv": (str(worker), str(directory))})
        path.write_text(bootstrap.model_dump_json())
        return original_spawn(trial_directory, argv)

    with patch.object(pybeam_reference, "spawn_owned", injected_spawn):
        output = pybeam_reference.run(directory)
    (directory / "adapter.json").write_text(output.model_dump_json() + "\n")


def main() -> None:
    fault = Fault.model_validate_json(sys.argv[1])
    directory = Path(sys.argv[2])
    (directory / "identity-fault.json").write_text(fault.model_dump_json())
    match fault.backend:
        case "python":
            inject_python(directory, fault)
        case "pybeam":
            inject_pybeam(directory, fault)
        case "smoke":
            inject_smoke(directory, fault)


if __name__ == "__main__":
    main()
