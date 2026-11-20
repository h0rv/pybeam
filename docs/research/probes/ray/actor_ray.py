import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Protocol

import ray
from failure_workload import wait_file
from ray.actor import ActorHandle
from ray.exceptions import ActorDiedError, ActorUnavailableError


class IntCall(Protocol):
    def remote(self, amount: int) -> ray.ObjectRef[int]: ...


class NoArgCall(Protocol):
    def remote(self) -> ray.ObjectRef[int]: ...


class Handle(Protocol):
    @property
    def add(self) -> IntCall: ...
    @property
    def wait(self) -> NoArgCall: ...
    @property
    def pid(self) -> NoArgCall: ...


class Counter:
    def __init__(self) -> None:
        self.value = 7

    def add(self, amount: int) -> int:
        self.value += amount
        return self.value

    def wait(self) -> int:
        self.value = 99
        Path("entered").touch()
        time.sleep(2)
        return self.value

    def pid(self) -> int:
        return os.getpid()


async def observe(counter: Handle) -> str:
    try:
        await counter.wait.remote()
    except Exception as error:
        return type(error).__name__
    return "returned"


async def main() -> dict[str, object]:
    ray.init(
        num_cpus=1, include_dashboard=False, log_to_driver=False, object_store_memory=256 * 2**20
    )
    factory = ray.remote(num_cpus=0, max_restarts=-1, max_task_retries=0)(Counter)
    native = factory.remote()
    if not isinstance(native, ActorHandle):
        raise TypeError("Expected a native actor handle")
    counter: Handle = native
    before = await counter.add.remote(2)
    old_pid = await counter.pid.remote()
    async with asyncio.timeout(45), asyncio.TaskGroup() as calls:
        inflight = calls.create_task(observe(counter))
        await asyncio.to_thread(wait_file, Path("entered"))
        started = time.perf_counter()
        ray.kill(native, no_restart=False)
        retry_errors: list[str] = []
        while True:
            try:
                state = await counter.add.remote(0)
                break
            except (ActorDiedError, ActorUnavailableError) as error:
                retry_errors.append(type(error).__name__)
                await asyncio.sleep(0.02)
        recovery = time.perf_counter() - started
    new_pid = await counter.pid.remote()
    if old_pid == new_pid or state != 7 or inflight.result() == "returned":
        raise AssertionError("Actor process did not crash and reset")
    return {
        "probe": "actor_crash",
        "framework": "ray",
        "trial": int(os.environ.get("PROBE_TRIAL", "1")),
        "recovery_s": recovery,
        "state_before": before,
        "state_midcall": 99,
        "state_after": state,
        "inflight": inflight.result(),
        "identity_changed": old_pid != new_pid,
        "kill": "ray.kill(no_restart=False)",
        "max_restarts": -1,
        "max_task_retries": 0,
        "rejoin": "same handle; explicit read-only polling",
        "recovery_call_errors": retry_errors,
        "infra": "local Ray cluster, actor worker process; no external service",
    }


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(main())
    finally:
        ray.shutdown()
    print(json.dumps(result), flush=True)
