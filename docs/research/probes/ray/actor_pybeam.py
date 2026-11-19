import asyncio
import json
import os
import time
from pathlib import Path

from failure_workload import wait_file

import pybeam


@pybeam.actor
class Counter:
    value: int = 7

    async def add(self, amount: int) -> int:
        self.value += amount
        return self.value

    async def wait(self) -> int:
        self.value = 99
        await asyncio.to_thread(Path("entered").touch)
        await asyncio.sleep(2)
        return self.value


async def observe(counter: Counter) -> str:
    try:
        await counter.wait()
    except Exception as error:
        return type(error).__name__
    return "returned"


async def main() -> None:
    async with asyncio.timeout(45), pybeam.ActorGroup() as group:
        counter = await group.spawn(Counter)
        before = await counter.add(2)
        old_pid = (await group.info(counter)).pid
        async with asyncio.TaskGroup() as calls:
            inflight = calls.create_task(observe(counter))
            await asyncio.to_thread(wait_file, Path("entered"))
            started = time.perf_counter()
            await group.crash(counter)
            fresh = await group.rejoin(counter)
            state = await fresh.add(0)
            recovery = time.perf_counter() - started
        new_pid = (await group.info(fresh)).pid
        if old_pid == new_pid or state != 7 or inflight.result() == "returned":
            raise AssertionError("Actor generation did not crash and reset")
    print(
        json.dumps(
            {
                "probe": "actor_crash",
                "framework": "pybeam",
                "trial": int(os.environ.get("PROBE_TRIAL", "1")),
                "recovery_s": recovery,
                "state_before": before,
                "state_midcall": 99,
                "state_after": state,
                "inflight": inflight.result(),
                "identity_changed": old_pid != new_pid,
                "kill": "ActorGroup.crash: exit(Pid, kill)",
                "rejoin": "explicit",
                "infra": "managed launcher, guardian, BEAM; no external service",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    pybeam.run(main)
