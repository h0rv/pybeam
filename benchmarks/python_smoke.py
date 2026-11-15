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
    Phases,
    Receipt,
    Runtime,
    Unsupported,
    messages,
)


@dataclass(frozen=True, slots=True)
class Pending:
    message: Message
    done: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class Observation:
    id: int
    entity: int
    sent_ns: int
    completed_ns: int


@dataclass(slots=True)
class Observed:
    queue_peak: int = 0
    inflight: int = 0
    inflight_peak: int = 0


async def run(config: Config) -> AdapterResult | Unsupported:
    w = config.workload
    if (
        config.backend != "python"
        or w.id != "rooms.local"
        or w.activity != "uniform"
        or w.payload.kind != "scalar"
        or w.payload.encoding != "json-utf8"
        or w.payload.encoded_bytes != 1
        or w.payload.batch_size != 1
        or config.unsafe_native_crash
    ):
        return Unsupported(
            reason="This adapter implements only the uniform local scalar smoke contract",
            features=("requested workload or payload",),
        )
    warmup_messages = messages(w, w.warmup_events)
    measured_messages = messages(w, w.events)
    start = time.monotonic_ns()
    queues = [asyncio.Queue[Pending | None](maxsize=w.queue_capacity) for _ in range(w.entities)]
    counts = [0] * w.entities
    observed = Observed()
    permits = asyncio.Semaphore(w.max_inflight)

    async def worker(entity: int) -> None:
        queue = queues[entity]
        while True:
            pending = await queue.get()
            try:
                if pending is None:
                    return
                counts[entity] += pending.message.increment
                pending.done.set_result(None)
            finally:
                queue.task_done()

    async def invoke(message: Message, sent: int, receipts: list[Observation]) -> None:
        try:
            future = asyncio.get_running_loop().create_future()
            await queues[message.entity].put(Pending(message, future))
            observed.queue_peak = max(observed.queue_peak, queues[message.entity].qsize())
            await future
            receipts.append(
                Observation(
                    id=message.id,
                    entity=message.entity,
                    sent_ns=sent,
                    completed_ns=time.monotonic_ns(),
                )
            )
        finally:
            observed.inflight -= 1
            permits.release()

    async def produce(sequence: tuple[Message, ...]) -> tuple[Observation, ...]:
        receipts: list[Observation] = []
        async with asyncio.TaskGroup() as group:
            for message in sequence:
                sent = time.monotonic_ns()
                await permits.acquire()
                observed.inflight += 1
                observed.inflight_peak = max(observed.inflight_peak, observed.inflight)
                group.create_task(invoke(message, sent, receipts))
        return tuple(receipts)

    worker_tasks: list[asyncio.Task[None]] = []
    async with asyncio.timeout(config.budget.wall_seconds), asyncio.TaskGroup() as group:
        for entity in range(w.entities):
            worker_tasks.append(group.create_task(worker(entity)))
        await asyncio.sleep(0)
        creation = time.monotonic_ns() - start
        start = time.monotonic_ns()
        await produce(warmup_messages)
        warmup = time.monotonic_ns() - start
        counts[:] = [0] * w.entities
        observed = Observed()
        start = time.monotonic_ns()
        observations = await produce(measured_messages)
        measurement = time.monotonic_ns() - start
        terminal = tuple(counts)
        start = time.monotonic_ns()
        for queue in queues:
            await queue.put(None)
    deletion = time.monotonic_ns() - start
    return AdapterResult(
        backend="python",
        workload=w,
        runtime=Runtime(
            python=sys.version,
            otp=None,
            erts=None,
            elixir=None,
            mode="asyncio-taskgroup",
            gil_enabled=sys._is_gil_enabled(),
            interpreters=1,
            workers=1,
            schedulers=None,
            executable=sys.executable,
            executable_sha256=hashlib.sha256(
                await asyncio.to_thread(Path(sys.executable).read_bytes)
            ).hexdigest(),
            native_source_sha=None,
        ),
        phases=Phases(
            creation_ns=creation,
            warmup_ns=warmup,
            measurement_ns=measurement,
            deletion_ns=deletion,
        ),
        receipts=tuple(
            Receipt(id=r.id, entity=r.entity, sent_ns=r.sent_ns, completed_ns=r.completed_ns)
            for r in observations
        ),
        transport_encoding="in-process Python objects; no serialization",
        transport_payload_bytes=None,
        terminal_counts=terminal,
        entities_created=len(worker_tasks),
        entities_deleted=sum(task.done() for task in worker_tasks),
        queue_peak=observed.queue_peak,
        inflight_peak=observed.inflight_peak,
        failures=sum(task.exception() is not None for task in worker_tasks),
        recovered=None,
        lost=w.events - len(observations),
        cleanup_acknowledged=all(task.done() for task in worker_tasks)
        and all(queue.empty() for queue in queues),
        semantic_differences=(
            "Asyncio tasks share one interpreter and event loop; not isolated OTP processes.",
            "Queue capacity is per destination; global admission includes queued and running work.",
        ),
        unsupported_features=("native actors", "supervision", "finite native actor admission"),
    )


def main() -> None:
    directory = Path(sys.argv[1])
    config = Config.model_validate_json((directory / "config.json").read_text())
    output = asyncio.run(run(config))
    (directory / "adapter.json").write_text(output.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
