# crash.py
import ctypes

import crash as entries

from pybeam import run, tasks
from pybeam.codec import Json


async def parse(payload: bytes) -> bytes:
    if Json(str).decode(payload) == "bad":
        ctypes.string_at(0)
    return payload


async def main() -> None:
    job = tasks.TaskFunction(
        entries.parse,
        Json(str),
        Json(str),
        execution=tasks.ExecutionMode.ISOLATED,
    )
    limits = tasks.TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=3000)
    async with tasks.TaskSupervisor(limits) as supervisor:
        try:
            await (await supervisor.start(job, "bad"))
        except tasks.TaskCrashed as error:
            print("Call crashed:", error.reason)
        print(await (await supervisor.start(job, "Still running")))


if __name__ == "__main__":
    run(main)
