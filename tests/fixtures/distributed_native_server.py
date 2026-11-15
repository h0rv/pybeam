import asyncio
import socket
import sys
from pathlib import Path
from typing import override

import pytest
import uvicorn
from pydantic import BaseModel, ConfigDict, field_validator

from benchmarks.contract import Config as RunConfig
from benchmarks.contract import Outcome, ResourceSample
from examples.distributed.models import Observation, WalkthroughProof
from examples.showcase import app as showcase
from examples.showcase import jobs as showcase_jobs
from examples.showcase.app import Settings, create_app
from examples.showcase.jobs import Job, Jobs
from pybeam.runtime.ownership import Registry, durable_write


class JobAudit(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: str
    status: str
    directory: Path
    observations: tuple[Observation, ...]
    proof: WalkthroughProof | None
    registry: Registry | None
    outcome: Outcome | None
    peak_sample: ResourceSample | None = None


class Audit(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    subscribers: int
    close_calls: int
    jobs: tuple[JobAudit, ...]


def serve(
    port: int, audit: Path, *, hold: bool = False, listener: socket.socket | None = None
) -> None:
    class NativeHoldConfig(RunConfig):
        @field_validator("adapter_command")
        @classmethod
        def fixed_test_observer(cls, command: tuple[str, ...]) -> tuple[str, ...]:
            if command == (sys.executable, "-m", "examples.showcase.distributed"):
                return (sys.executable, "-m", "tests.fixtures.distributed_native_adapter")
            return command

    class ObservedJobs(Jobs):
        close_calls = 0

        def record(self) -> None:
            jobs: list[JobAudit] = []
            for job in self.items.values():
                trial = job.directory / "result/trial-0"
                registry = trial / "ownership.json"
                outcome = trial / "outcome.json"
                resources = trial / "resources.jsonl"
                samples = (
                    tuple(
                        ResourceSample.model_validate_json(line)
                        for line in resources.read_text().splitlines(keepends=True)
                        if line.endswith("\n")
                    )
                    if resources.is_file()
                    else ()
                )
                if outcome.is_file() and resources.is_file():
                    durable_write(
                        audit.with_name(job.id + ".resources.jsonl"), resources.read_text()
                    )
                    for name in (
                        "config.json",
                        "stdout.log",
                        "stderr.log",
                        "ownership.json",
                        "outcome.json",
                        "native-ready.json",
                        "inner-cleanup.json",
                        "managed-cleanup.json",
                        "managed-startup-census.json",
                    ):
                        source = trial / name
                        if source.is_file():
                            durable_write(audit.with_name(job.id + "." + name), source.read_text())
                jobs.append(
                    JobAudit(
                        id=job.id,
                        status=job.status,
                        directory=job.directory,
                        observations=job.observations,
                        proof=job.proof,
                        registry=Registry.model_validate_json(registry.read_bytes())
                        if registry.is_file()
                        else None,
                        outcome=Outcome.model_validate_json(outcome.read_bytes())
                        if outcome.is_file()
                        else None,
                        peak_sample=max(
                            samples,
                            key=lambda sample: sum(p.threads for p in sample.processes),
                            default=None,
                        ),
                    )
                )
            durable_write(
                audit,
                Audit(
                    subscribers=len(self.subscribers),
                    close_calls=self.close_calls,
                    jobs=tuple(jobs),
                ).model_dump_json(),
            )

        @override
        def update(self, job: Job) -> None:
            super().update(job)
            self.record()

        @override
        def subscribe(self) -> asyncio.Queue[int]:
            result = super().subscribe()
            self.record()
            return result

        @override
        def unsubscribe(self, queue: asyncio.Queue[int]) -> None:
            super().unsubscribe(queue)
            self.record()

        @override
        async def close(self) -> None:
            self.close_calls += 1
            await super().close()
            self.record()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(showcase, "Jobs", ObservedJobs)
        if hold:
            patch.setattr(showcase_jobs, "Config", NativeHoldConfig)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(Settings(port=port)),
                host="127.0.0.1",
                port=port,
                access_log=False,
                timeout_graceful_shutdown=2,
            )
        )
        server.run(sockets=None if listener is None else [listener])


if __name__ == "__main__":
    if len(sys.argv) == 5:
        with socket.socket(fileno=int(sys.argv[4])) as listener:
            serve(
                int(sys.argv[1]), Path(sys.argv[2]), hold=sys.argv[3] == "held", listener=listener
            )
    else:
        serve(int(sys.argv[1]), Path(sys.argv[2]), hold=sys.argv[3:] == ["held"])
