import asyncio
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from examples.showcase.__main__ import main
from examples.showcase.jobs import Job, Jobs
from pybeam.codec import Json
from pybeam.runtime.ownership import Identity, identity


@dataclass(frozen=True, slots=True, kw_only=True)
class CloseEvidence:
    closing: bool
    subscribers: int
    stop_requests: tuple[bool, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class JobEvidence:
    status: Literal["stopped", "uncertain"]
    birth: Identity
    cleanup_acknowledged: bool
    returncode: int


CHILD = "\n".join(
    (
        "import sys, time",
        "from pathlib import Path",
        "stop, ack = (Path(value) for value in sys.argv[1:])",
        "deadline = time.monotonic() + 20",
        "print('READY', flush=True)",
        "while not stop.exists():",
        "    if time.monotonic() >= deadline: raise SystemExit(2)",
        "    time.sleep(0.01)",
        "ack.write_text('cleanup acknowledged')",
    )
)


def run() -> None:
    directory = Path(sys.argv.pop(1))
    original_close = Jobs.close
    original_unsubscribe = Jobs.unsubscribe

    async def close(self: Jobs) -> None:
        await original_close(self)
        evidence = CloseEvidence(
            closing=self.closing,
            subscribers=len(self.subscribers),
            stop_requests=tuple(
                (job.directory / "stop.request").exists() for job in self.items.values()
            ),
        )
        (directory / "close.json").write_text(Json(type(evidence)).encode(evidence).decode())

    def unsubscribe(self: Jobs, queue: asyncio.Queue[int]) -> None:
        original_unsubscribe(self, queue)
        (directory / "subscribers-after-disconnect.txt").write_text(str(len(self.subscribers)))

    async def execute(self: Jobs, job: Job) -> None:
        # Controlled non-Native ownership boundary only. Production admission, stop,
        # lifespan TaskGroup and HTTP/SSE routes remain unchanged.
        async with self.serial:
            stop = job.directory / "stop.request"
            ack = job.directory / "cleanup.ack"
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                CHILD,
                str(stop),
                str(ack),
                stdout=asyncio.subprocess.PIPE,
            )
            birth = identity(process.pid)
            if process.stdout is None or await process.stdout.readline() != b"READY\n":
                raise RuntimeError("Controlled owned Python child did not start")
            self.update(replace(self.items[job.id], status="running"))
            (directory / "child-ready.txt").write_text(Json(type(birth)).encode(birth).decode())
            returncode = await process.wait()
            acknowledged = ack.is_file() and ack.read_text() == "cleanup acknowledged"
            status = (
                "stopped" if returncode == 0 and stop.exists() and acknowledged else "uncertain"
            )
            self.update(replace(self.items[job.id], status=status))
            evidence = JobEvidence(
                status=status,
                birth=birth,
                cleanup_acknowledged=acknowledged,
                returncode=returncode,
            )
            (directory / "job-finished.json").write_text(
                Json(type(evidence)).encode(evidence).decode()
            )

    with (
        patch.object(Jobs, "close", close),
        patch.object(Jobs, "unsubscribe", unsubscribe),
        patch.object(Jobs, "execute", execute),
    ):
        main()


if __name__ == "__main__":
    run()
