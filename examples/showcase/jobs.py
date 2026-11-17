import asyncio
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, assert_never

from benchmarks.contract import AdapterResult, Budget, Config, Workload
from benchmarks.ownership import Control, Registry, identity
from benchmarks.profiles import configuration, unavailable
from benchmarks.provenance import Provenance
from benchmarks.runner import TrialSummary
from examples.distributed.models import (
    OBSERVATION,
    CleanupObserved,
    ExchangeObserved,
    NodeObserved,
    Observation,
    StateObserved,
    WalkthroughProof,
)
from examples.distributed.policy import walkthrough_budget, walkthrough_stop
from examples.showcase.catalog import ROOT, Card, distributed_unavailable
from pybeam.codec import Json


@dataclass(frozen=True)
class NodeView:
    observation: NodeObserved
    status: Literal["observed", "fresh", "down_uncertain", "cleanup_confirmed", "cleanup_uncertain"]


@dataclass(frozen=True)
class Job:
    id: str
    card: Card
    directory: Path
    status: Literal[
        "queued", "running", "finished", "failed", "stopping", "stopped", "uncertain"
    ] = "queued"
    error: str = ""
    trials: tuple[TrialSummary, ...] = ()
    provenance: Provenance | None = None
    results: tuple[AdapterResult, ...] = ()
    observations: tuple[Observation, ...] = ()
    proof: WalkthroughProof | None = None

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running", "stopping"}

    @property
    def nodes(self) -> tuple[NodeView, ...]:
        nodes: dict[str, NodeView] = {}
        for event in self.observations:
            match event:
                case NodeObserved():
                    nodes[event.alias] = NodeView(event, event.status)
                case StateObserved(state="nodedown_uncertain"):
                    if "b" in nodes:
                        nodes["b"] = replace(nodes["b"], status="down_uncertain")
                case CleanupObserved():
                    status = (
                        "cleanup_confirmed" if event.state == "confirmed" else "cleanup_uncertain"
                    )
                    nodes = {alias: replace(node, status=status) for alias, node in nodes.items()}
                case StateObserved() | ExchangeObserved():
                    pass
                case _:
                    assert_never(event)
        return tuple(nodes[alias] for alias in ("a", "b") if alias in nodes)

    def logs(self) -> str:
        if self.card.key == "distributed":
            return "Only sanitized observations are available for this walkthrough."
        paths = [self.directory / "runner.log"]
        trial = self.directory / "result/trial-0"
        paths.extend(trial / name for name in ("stdout.log", "stderr.log", "1.stdout.log"))
        chunks: list[str] = []
        for path in paths:
            if path.is_file():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 4096))
                    chunks.append(path.name + ":\n" + stream.read(4096).decode(errors="replace"))
        return "\n".join(chunks)[-16384:]


class Jobs:
    def __init__(
        self, group: asyncio.TaskGroup, directory: Path, native: Path | None = None
    ) -> None:
        self.native = native
        self.group = group
        self.directory = directory
        self.items: dict[str, Job] = {}
        self.serial = asyncio.Semaphore(1)
        self.closing = False
        self.revision = 0
        self.subscribers: set[asyncio.Queue[int]] = set()

    def changed(self) -> None:
        self.revision += 1
        for queue in self.subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(self.revision)

    def subscribe(self) -> asyncio.Queue[int]:
        if self.closing or len(self.subscribers) >= 16:
            raise ValueError("At most 16 event streams per session")
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        self.subscribers.add(queue)
        queue.put_nowait(self.revision)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[int]) -> None:
        self.subscribers.discard(queue)

    def update(self, job: Job) -> None:
        if self.items[job.id] != job:
            self.items[job.id] = job
            self.changed()

    def start(self, card: Card) -> Job:
        if self.closing or len(self.items) >= 16 or sum(j.active for j in self.items.values()) >= 4:
            raise ValueError(
                "Session limit: 16 retained jobs, four queued/running. Restart to clear."
            )
        if card.disabled is not None:
            raise ValueError(card.disabled)
        key = uuid.uuid4().hex
        directory = self.directory / key
        directory.mkdir()
        job = Job(key, card, directory)
        self.items[key] = job
        self.changed()
        self.group.create_task(self.execute(job))
        return job

    def stop(self, job: Job) -> None:
        current = self.items[job.id]
        if current.active and current.status != "stopping":
            (current.directory / "stop.request").touch()
            self.update(replace(current, status="stopping"))

    async def close(self) -> None:
        self.closing = True
        self.changed()
        for job in self.items.values():
            self.stop(job)

    async def execute(self, job: Job) -> None:
        async with self.serial:
            job = self.items[job.id]
            if job.status == "stopping":
                self.update(replace(job, status="stopped"))
                return
            job = replace(job, status="running")
            self.update(job)
            config = Config(
                python_source=str(ROOT),
                native_source=None,
                repeats=1,
                workload=Workload(entities=4, events=100, warmup_events=10),
                budget=Budget(wall_seconds=30.0, artifact_bytes=2_097_152),
                adapter_command=()
                if job.card.key == "smoke"
                else (
                    sys.executable,
                    "-m",
                    "examples.showcase.demo",
                    job.card.key,
                    "" if job.card.launcher is None else str(job.card.launcher),
                ),
            )
            if job.card.benchmark is not None:
                reason = unavailable(job.card.benchmark, self.native)
                if reason is not None:
                    self.update(replace(job, error=reason, status="failed"))
                    return
                config = configuration(job.card.benchmark, self.native)
            if job.card.key == "distributed":
                reason = distributed_unavailable()
                if reason is not None:
                    self.update(replace(job, error=reason, status="failed"))
                    return
                config = Config(
                    python_source=str(ROOT),
                    repeats=1,
                    purpose="walkthrough",
                    stop_policy=walkthrough_stop(),
                    budget=walkthrough_budget(),
                    adapter_command=(sys.executable, "-m", "examples.showcase.distributed"),
                )
            (job.directory / "config.json").write_text(config.model_dump_json())
            control = Control(
                owner=identity(os.getpid()), stop_file=str(job.directory / "stop.request")
            )
            command = (
                sys.executable
                if job.card.benchmark is None
                else str(ROOT / "benchmarks/.venv/bin/python"),
                "-m",
                "benchmarks",
                "run",
                "--config",
                str(job.directory / "config.json"),
                "--output",
                str(job.directory / "result"),
                "--control",
                control.model_dump_json(),
            )
            try:
                with (job.directory / "runner.log").open("wb") as log:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=ROOT,
                        stdout=log,
                        stderr=log,
                    )
                    # Runner owns deadlines; guardian also watches this UI's PID and creation time.
                    if job.card.key == "distributed":
                        async with asyncio.TaskGroup() as group:
                            group.create_task(self.observe(job, process))
                            await process.wait()
                    else:
                        await process.wait()
                job = self.items[job.id]
                path = job.directory / "result/trials.jsonl"
                if path.exists():
                    job = replace(
                        job,
                        trials=tuple(
                            TrialSummary.model_validate_json(line)
                            for line in path.read_text().splitlines()
                        ),
                    )
                provenance = job.directory / "result/provenance.json"
                if provenance.is_file():
                    job = replace(
                        job, provenance=Provenance.model_validate_json(provenance.read_text())
                    )
                job = replace(
                    job,
                    results=tuple(
                        AdapterResult.model_validate_json(
                            (
                                job.directory
                                / "result"
                                / ("trial-" + str(t.trial))
                                / "adapter.json"
                            ).read_text()
                        )
                        for t in job.trials
                        if t.outcome.status == "passed"
                    ),
                )
                if any(t.outcome.status == "walkthrough" for t in job.trials):
                    proof = Json(WalkthroughProof).decode(
                        (job.directory / "result/trial-0/adapter.json").read_text()
                    )
                    if proof.observations != job.observations:
                        raise ValueError("Proof differs from observed history")
                    job = replace(job, proof=proof)
                registry_path = job.directory / "result/trial-0/ownership.json"
                uncertain = (
                    registry_path.exists()
                    and Json(Registry).decode(registry_path.read_text()).phase != "confirmed"
                )
                status = (
                    "uncertain"
                    if uncertain
                    or process.returncode == 3
                    or any(trial.outcome.cleanup_state == "uncertain" for trial in job.trials)
                    else (
                        "stopped"
                        if (job.directory / "stop.request").exists()
                        else ("finished" if process.returncode == 0 else "failed")
                    )
                )
                self.update(replace(job, status=status))
            except (OSError, ValueError) as error:
                current = self.items[job.id]
                self.update(
                    replace(
                        current,
                        error="Invalid walkthrough evidence or startup failure"
                        if job.card.key == "distributed"
                        else str(error),
                        status="uncertain" if (job.directory / "result").exists() else "failed",
                    )
                )

    async def observe(self, job: Job, process: asyncio.subprocess.Process) -> None:
        path = job.directory / "result/trial-0/observations.jsonl"
        consumed: list[bytes] = []
        try:
            while True:
                if path.is_file():
                    if path.stat().st_size > 65536:
                        raise ValueError("Observation byte budget exceeded")
                    with path.open("rb") as stream:
                        payload = stream.read(65537)
                    lines = payload.split(b"\n")[:-1]
                    if len(lines) > 16 or lines[: len(consumed)] != consumed:
                        raise ValueError("Observation count or append-only contract violated")
                    for line in lines[len(consumed) :]:
                        event = OBSERVATION.decode(line)
                        current = self.items[job.id]
                        self.update(replace(current, observations=(*current.observations, event)))
                        consumed.append(line)
                if process.returncode is not None:
                    return
                await asyncio.sleep(0.1)
        except OSError, ValueError:
            self.stop(self.items[job.id])
            self.update(replace(self.items[job.id], error="Invalid bounded observation stream"))


def session_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="pybeam-ui-", dir="/tmp", delete=False)
