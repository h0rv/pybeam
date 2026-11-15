import asyncio
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil

from benchmarks.contract import Budget, Config, Workload
from benchmarks.ownership import Control, Registry, identity
from benchmarks.runner import TrialSummary
from examples.showcase.catalog import ROOT, Card


@dataclass
class Job:
    id: str
    card: Card
    directory: Path
    status: Literal[
        "queued", "running", "finished", "failed", "stopping", "stopped", "uncertain"
    ] = "queued"
    error: str = ""
    trials: tuple[TrialSummary, ...] = ()

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running", "stopping"}

    def logs(self) -> str:
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
    def __init__(self, group: asyncio.TaskGroup, directory: Path) -> None:
        self.group = group
        self.directory = directory
        self.items: dict[str, Job] = {}
        self.serial = asyncio.Semaphore(1)
        self.closing = False

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
        self.group.create_task(self.execute(job))
        return job

    def stop(self, job: Job) -> None:
        if job.active:
            (job.directory / "stop.request").touch()
            job.status = "stopping"

    async def close(self) -> None:
        self.closing = True
        for job in self.items.values():
            self.stop(job)

    async def execute(self, job: Job) -> None:
        async with self.serial:
            if job.status == "stopping":
                job.status = "stopped"
                return
            job.status = "running"
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
            (job.directory / "config.json").write_text(config.model_dump_json())
            control = Control(
                owner=identity(psutil.Process()), stop_file=str(job.directory / "stop.request")
            )
            command = (
                sys.executable,
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
                    await process.wait()
                path = job.directory / "result/trials.jsonl"
                if path.exists():
                    job.trials = tuple(
                        TrialSummary.model_validate_json(line)
                        for line in path.read_text().splitlines()
                    )
                registry_path = job.directory / "result/trial-0/ownership.json"
                uncertain = (
                    registry_path.exists()
                    and Registry.model_validate_json(registry_path.read_text()).phase != "confirmed"
                )
                job.status = (
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
            except (OSError, ValueError) as error:
                job.error = str(error)
                job.status = "uncertain" if (job.directory / "result").exists() else "failed"


def session_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="pybeam-ui-", dir="/tmp", delete=False)
