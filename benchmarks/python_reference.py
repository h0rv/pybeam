import asyncio
import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from benchmarks.contract import (
    AdapterResult,
    Config,
    Message,
    Payload,
    Phases,
    Receipt,
    Runtime,
    Unsupported,
    messages,
)


@dataclass(frozen=True, slots=True)
class Pending:
    message: Message
    applied: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class Observation:
    id: int
    entity: int
    sent_ns: int
    completed_ns: int


@dataclass(slots=True)
class Peaks:
    queue: int = 0
    inflight: int = 0
    inflight_peak: int = 0


async def worker(entity: int, queue: asyncio.Queue[Pending | None], counts: list[int]) -> None:
    while True:
        pending = await queue.get()
        try:
            if pending is None:
                return
            counts[entity] += pending.message.increment
            pending.applied.set_result(None)
        finally:
            queue.task_done()


async def invoke(
    message: Message,
    sent_ns: int,
    queue: asyncio.Queue[Pending | None],
    peaks: Peaks,
    observations: list[Observation],
) -> None:
    try:
        applied = asyncio.get_running_loop().create_future()
        await queue.put(Pending(message, applied))
        peaks.queue = max(peaks.queue, queue.qsize())
        await applied
        observations.append(Observation(message.id, message.entity, sent_ns, time.monotonic_ns()))
    finally:
        peaks.inflight -= 1


async def dispatch(
    sequence: tuple[Message, ...],
    queues: list[asyncio.Queue[Pending | None]],
    window: int,
    peaks: Peaks,
) -> tuple[Observation, ...]:
    observations: list[Observation] = []
    active: set[asyncio.Task[None]] = set()
    async with asyncio.TaskGroup() as group:
        for message in sequence:
            # Timestamp before admission, including time waiting for a window slot.
            sent_ns = time.monotonic_ns()
            if len(active) >= window:
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                active.difference_update(done)
                for task in done:
                    task.result()
            peaks.inflight += 1
            peaks.inflight_peak = max(peaks.inflight_peak, peaks.inflight)
            active.add(
                group.create_task(
                    invoke(message, sent_ns, queues[message.entity], peaks, observations)
                )
            )
    return tuple(observations)


def runtime_metadata() -> Runtime:
    return Runtime(
        python=sys.version,
        otp=None,
        erts=None,
        elixir=None,
        mode="python-reference: asyncio TaskGroup; one event-loop thread",
        gil_enabled=sys._is_gil_enabled(),
        interpreters=1,
        workers=1,
        schedulers=None,
        executable=sys.executable,
        executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        native_source_sha=None,
    )


async def run(config: Config) -> AdapterResult | Unsupported:
    w = config.workload
    if (
        config.backend != "python"
        or w.id != "rooms.local"
        or w.activity != "uniform"
        or w.payload != Payload()
        or config.unsafe_native_crash
    ):
        return Unsupported(
            reason="Python reference supports only uniform rooms.local scalar increment one",
            features=("requested backend, workload, payload, or unsafe crash",),
        )
    if sys.version_info[:3] != (3, 14, 10) or not sys._is_gil_enabled():
        return Unsupported(
            reason="This reference baseline requires conventional GIL CPython 3.14.10",
            features=("execution mode",),
        )

    warmup_sequence = messages(w, w.warmup_events)
    measured_sequence = messages(w, w.events)
    expected_warmup = tuple(
        sum(message.entity == entity for message in warmup_sequence) for entity in range(w.entities)
    )
    peaks = Peaks()
    start = time.monotonic_ns()
    queues = [asyncio.Queue[Pending | None](maxsize=w.queue_capacity) for _ in range(w.entities)]
    counts = [0] * w.entities
    tasks: list[asyncio.Task[None]] = []
    async with asyncio.timeout(config.budget.wall_seconds), asyncio.TaskGroup() as group:
        for entity, queue in enumerate(queues):
            tasks.append(group.create_task(worker(entity, queue, counts)))
        await asyncio.sleep(0)
        creation_ns = time.monotonic_ns() - start

        start = time.monotonic_ns()
        await dispatch(warmup_sequence, queues, w.max_inflight, peaks)
        warmup_counts = tuple(counts)
        if warmup_counts != expected_warmup:
            raise ValueError("Warmup snapshot differs from applied messages")
        counts[:] = [0] * w.entities
        if any(counts):
            raise ValueError("Warmup reset left nonzero state")
        warmup_ns = time.monotonic_ns() - start
        peaks = Peaks()

        start = time.monotonic_ns()
        observations = await dispatch(measured_sequence, queues, w.max_inflight, peaks)
        measurement_ns = time.monotonic_ns() - start
        terminal_counts = tuple(counts)

        start = time.monotonic_ns()
        for queue in queues:
            await queue.put(None)
    deletion_ns = time.monotonic_ns() - start

    return AdapterResult(
        backend="python",
        workload=w,
        runtime=runtime_metadata(),
        phases=Phases(
            creation_ns=creation_ns,
            warmup_ns=warmup_ns,
            measurement_ns=measurement_ns,
            deletion_ns=deletion_ns,
        ),
        receipts=tuple(
            Receipt(id=r.id, entity=r.entity, sent_ns=r.sent_ns, completed_ns=r.completed_ns)
            for r in observations
        ),
        transport_encoding=(
            "in-process Python Message objects through asyncio.Queue; no serialization"
        ),
        transport_payload_bytes=None,
        terminal_counts=terminal_counts,
        entities_created=len(tasks),
        entities_deleted=sum(task.done() and not task.cancelled() for task in tasks),
        queue_peak=peaks.queue,
        inflight_peak=peaks.inflight_peak,
        failures=0,
        recovered=None,
        lost=w.events - len(observations),
        cleanup_acknowledged=all(task.done() and task.exception() is None for task in tasks)
        and all(queue.empty() for queue in queues),
        semantic_differences=(
            "Rooms are tasks sharing one interpreter and event loop, not isolated processes.",
            "Global window bounds queued and running calls, without precreating event tasks.",
            "Destination queues remain bounded even under client_admission_only; native actor "
            "mailboxes may remain unbounded. Compare only the common client admission guarantee.",
            "Deletion awaits every worker; it does not promise allocator memory release.",
        ),
        unsupported_features=("other workload families", "native isolation", "supervision"),
    )


def main() -> None:
    directory = Path(sys.argv[1])
    config = Config.model_validate_json((directory / "config.json").read_text())
    output = asyncio.run(run(config))
    (directory / "adapter.json").write_text(output.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
