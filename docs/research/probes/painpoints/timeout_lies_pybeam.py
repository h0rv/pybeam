# Rerun: ~/painpoint-venv/bin/python docs/research/probes/painpoints/timeout_lies_pybeam.py
import asyncio
import json
import tempfile
import time
from pathlib import Path

import timeout_lies_pybeam as entries

from pybeam import run
from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskCrashed, TaskFunction, TaskLimits, TaskSupervisor


def blocking_charge(path: Path) -> None:
    for _ in range(30):
        time.sleep(0.1)
        with path.open("a") as stream:
            stream.write(str(time.monotonic()) + "\n")


async def charge_card(payload: bytes) -> bytes:
    blocking_charge(Path(Json(str).decode(payload)))
    return Json(bytes).encode(b"done")


async def main() -> None:
    job = TaskFunction(
        entries.charge_card, Json(str), Json(bytes), execution=ExecutionMode.ISOLATED
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "effects"
        path.touch()
        async with TaskSupervisor(
            TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=3000)
        ) as supervisor:
            task = await supervisor.start(job, str(path))
            for _ in range(1000):
                if path.stat().st_size:
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("Isolated task never wrote its first side effect")
            await asyncio.sleep(0.5)
            timed_out = time.monotonic()
            await task.terminate()
            error = await task.exception()
            if not isinstance(error, TaskCrashed):
                raise RuntimeError("Expected terminated isolated task, got " + repr(error))
            terminate_s = time.monotonic() - timed_out
            await asyncio.sleep(3)
            effects = [float(line) for line in path.read_text().splitlines()]
            print(
                json.dumps(
                    {
                        "case": "terminate",
                        "terminate_s": terminate_s,
                        "effects_after_terminate_requested": sum(at > timed_out for at in effects),
                        "effects_total": len(effects),
                    }
                )
            )


if __name__ == "__main__":
    run(main)
