import asyncio
import gc
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from weakref import ReferenceType, ref

from pybeam import run
from pybeam.actors import ActorRef, ActorSpec, OwnedActorRef, UnboundedMailbox, spawn

from benchmarks.contract import (
    AdapterResult,
    Config,
    Message,
    Phases,
    Receipt,
    Runtime,
    StrictModel,
    messages,
)
from benchmarks.pybeam_reference_build import BuildManifest, asset


class Inventory(StrictModel):
    otp: str
    erts: str
    schedulers: int
    executable: str
    contexts: tuple[str, ...]
    actors: tuple[str, ...]
    loaded_beams: tuple[str, ...]


class WorkerEvidence(StrictModel):
    baseline: Inventory
    active: Inventory
    stopped: Inventory
    warmup_counts: tuple[int, ...]
    reset_counts: tuple[int, ...]
    python: str
    gil_enabled: bool
    interpreter_id: int
    interpreter_count: int
    context_threads_are_not_interpreters: bool = True
    actor_state_collected: bool


@dataclass(slots=True, weakref_slot=True)
class Room:
    instances: ClassVar[list[ReferenceType[Room]]] = []
    count: int = 0

    def __post_init__(self) -> None:
        self.instances.append(ref(self))

    async def add(self, increment: int) -> int:
        self.count += increment
        return self.count

    async def reset(self) -> None:
        self.count = 0

    async def read(self) -> int:
        return self.count


@dataclass(frozen=True, slots=True)
class Observation:
    id: int
    entity: int
    sent_ns: int
    completed_ns: int


@dataclass(slots=True)
class Observed:
    inflight: int = 0
    peak: int = 0


async def inventory() -> Inventory:
    import erlang

    raw = await erlang.async_call("_bench_inventory")
    if not isinstance(raw, (str, bytes)):
        raise TypeError("Native inventory callback must return JSON")
    return Inventory.model_validate_json(raw)


async def produce(
    actors: Sequence[ActorRef[Room]],
    sequence: tuple[Message, ...],
    limit: int,
) -> tuple[tuple[Observation, ...], int]:
    permits = asyncio.Semaphore(limit)
    observed = Observed()
    receipts: list[Observation] = []

    async def invoke(message: Message, sent: int) -> None:
        try:
            applied_count = await actors[message.entity].call(Room.add, message.increment)
            if applied_count != message.id // len(actors) + 1:
                raise ValueError("Invalid applied acknowledgment")
            receipts.append(
                Observation(
                    message.id,
                    message.entity,
                    sent,
                    time.monotonic_ns(),
                )
            )
        finally:
            observed.inflight -= 1
            permits.release()

    async with asyncio.TaskGroup() as group:
        for message in sequence:
            sent = time.monotonic_ns()
            await permits.acquire()
            observed.inflight += 1
            observed.peak = max(observed.peak, observed.inflight)
            group.create_task(invoke(message, sent))
    return tuple(receipts), observed.peak


async def measure(directory: Path) -> AdapterResult:
    import _interpreters

    config = Config.model_validate_json((directory / "config.json").read_text())
    manifest = BuildManifest.model_validate_json(
        (directory / "native-build-manifest.json").read_text()
    )
    w = config.workload
    warmup_sequence = messages(w, w.warmup_events)
    measured_sequence = messages(w, w.events)
    expected_warmup = tuple(
        sum(message.entity == entity for message in warmup_sequence) for entity in range(w.entities)
    )
    baseline = await inventory()
    if baseline.actors:
        raise RuntimeError("Unexpected existing actors in fresh VM")
    spec = ActorSpec(Room, methods=(Room.add, Room.read, Room.reset))
    actors: list[OwnedActorRef[Room]] = []
    start = time.monotonic_ns()
    try:
        for _ in range(w.entities):
            actors.append(await spawn(spec, mailbox=UnboundedMailbox()))
        creation = time.monotonic_ns() - start
        active = await inventory()
        if (
            len(active.actors) != w.entities
            or len(active.contexts) != len(baseline.contexts) + w.entities
        ):
            raise RuntimeError("Native actor PID/context inventory does not match entities")
        start = time.monotonic_ns()
        await produce(actors, warmup_sequence, w.max_inflight)
        warmup_counts = tuple([await actor.call(Room.read) for actor in actors])
        if warmup_counts != expected_warmup:
            raise ValueError("Warmup snapshot differs from applied messages")
        for actor in actors:
            await actor.call(Room.reset)
        reset_counts = tuple([await actor.call(Room.read) for actor in actors])
        if any(reset_counts):
            raise ValueError("Warmup reset left nonzero state")
        warmup = time.monotonic_ns() - start
        start = time.monotonic_ns()
        observations, peak = await produce(actors, measured_sequence, w.max_inflight)
        measurement = time.monotonic_ns() - start
        terminal = tuple([await actor.call(Room.read) for actor in actors])
    finally:
        start = time.monotonic_ns()
        for actor in reversed(actors):
            await actor.stop()
        deletion = time.monotonic_ns() - start
    stopped = await inventory()
    if stopped.actors or stopped.contexts != baseline.contexts:
        raise RuntimeError("Stopped actors left native PIDs or contexts behind")
    gc.collect()
    collected = all(instance() is None for instance in Room.instances)
    if not collected:
        raise RuntimeError("Native actor state remains retained after deletion")
    interpreter_count = len(_interpreters.list_all())
    evidence = WorkerEvidence(
        baseline=baseline,
        active=active,
        stopped=stopped,
        warmup_counts=warmup_counts,
        reset_counts=reset_counts,
        python=sys.version,
        gil_enabled=sys._is_gil_enabled(),
        interpreter_id=_interpreters.get_current()[0],
        interpreter_count=interpreter_count,
        actor_state_collected=collected,
    )
    (directory / "worker-evidence.json").write_text(evidence.model_dump_json(indent=2))
    loaded = tuple(
        asset(Path(path))
        for path in (
            str(Path(config.native_source) / "priv/py_nif.so"),
            *stopped.loaded_beams,
        )
    )
    if any(item not in manifest.assets for item in loaded):
        raise ValueError("Actual loaded native assets do not match the build manifest")
    executable = asset(Path(active.executable))
    return AdapterResult(
        backend="pybeam",
        workload=w,
        runtime=Runtime(
            python=sys.version,
            otp=active.otp,
            erts=active.erts,
            elixir=None,
            mode="legacy-unbounded-actor-prototype/worker/shared-main-interpreter",
            gil_enabled=sys._is_gil_enabled(),
            interpreters=interpreter_count,
            workers=len(active.contexts),
            schedulers=active.schedulers,
            executable=executable.path,
            executable_sha256=executable.sha256,
            native_source_sha=manifest.native.sha,
            loaded_assets=loaded,
        ),
        phases=Phases(
            creation_ns=creation,
            warmup_ns=warmup,
            measurement_ns=measurement,
            deletion_ns=deletion,
        ),
        receipts=tuple(
            Receipt(
                id=r.id,
                entity=r.entity,
                sent_ns=r.sent_ns,
                completed_ns=r.completed_ns,
            )
            for r in observations
        ),
        transport_encoding="retained_python_objects",
        transport_payload_bytes=None,
        terminal_counts=terminal,
        entities_created=len(actors),
        entities_deleted=len(actors),
        queue_peak=None,
        inflight_peak=peak,
        failures=0,
        recovered=None,
        lost=w.events - len(observations),
        cleanup_acknowledged=False,
        semantic_differences=(
            "Legacy raw backend, not the final Pythonic actor decorator API.",
            "Explicit UnboundedMailbox; only the global client window is finite.",
            "Retained immutable Python int payload; no encoded transport byte bound.",
            "Worker contexts share the main interpreter and GIL; not CPU cores.",
            "Both receipt endpoints are Python sender monotonic_ns; bridge work is included.",
            "Source-checkout experiment, not clean-wheel or public API usability evidence.",
            "Entity phases exclude VM startup, inventory checks, and terminal snapshots.",
        ),
        unsupported_features=(
            "finite destination queue",
            "raw transport input byte bound",
            "decorator backend",
            "supervision",
            "nonuniform workloads",
        ),
    )


async def main() -> None:
    directory = Path(sys.argv[1])
    output = await measure(directory)
    (directory / "worker-result.json").write_text(output.model_dump_json() + "\n")


if __name__ == "__main__":
    run(main)
