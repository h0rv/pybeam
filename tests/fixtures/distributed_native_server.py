import asyncio
import sys
from pathlib import Path
from typing import override

import pytest
import uvicorn
from pydantic import BaseModel, ConfigDict

from benchmarks.contract import Outcome
from examples.distributed.models import Observation, WalkthroughProof
from examples.showcase import app as showcase
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


class Audit(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    subscribers: int
    close_calls: int
    jobs: tuple[JobAudit, ...]


def serve(port: int, audit: Path) -> None:
    class ObservedJobs(Jobs):
        close_calls = 0

        def record(self) -> None:
            jobs: list[JobAudit] = []
            for job in self.items.values():
                trial = job.directory / "result/trial-0"
                registry = trial / "ownership.json"
                outcome = trial / "outcome.json"
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
        uvicorn.run(
            create_app(Settings(port=port)),
            host="127.0.0.1",
            port=port,
            access_log=False,
            timeout_graceful_shutdown=2,
        )


if __name__ == "__main__":
    serve(int(sys.argv[1]), Path(sys.argv[2]))
