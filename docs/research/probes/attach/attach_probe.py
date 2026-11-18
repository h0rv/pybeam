import os
import sys

import erlang

from pybeam.actors import ActorGroup, actor
from pybeam.codec import Json
from pybeam.integrations.hornbeam import Beam
from pybeam.runtime.loader import load, require_managed_entry
from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor


@actor
class Counter:
    value: int = 0

    async def add(self, amount: int) -> int:
        self.value += amount
        return self.value


async def double(value: int) -> int:
    return value * 2


async def actors() -> str:
    async with ActorGroup() as group:
        counter = await group.spawn(Counter, value=10)
        if await counter.add(2) != 12 or await counter.add(3) != 15:
            raise AssertionError("actor state mismatch")
    return "12 then 15; group closed"


async def tasks() -> str:
    function = TaskFunction(double, Json(int), Json(int))
    async with TaskSupervisor(TaskLimits(capacity=2, max_bytes=4096, shutdown_ms=5000)) as group:
        result = await group.start(function, 21)
        if await result != 42:
            raise AssertionError("task result mismatch")
    return "42; supervisor closed"


async def services() -> str:
    beam = await Beam.connect()
    if await beam.incr("attach", namespace="probe") != 1:
        raise AssertionError("counter mismatch")
    return "counter=1"


async def main(host: object, resource: object) -> dict[str, str]:
    results = {
        "python": sys.version.split()[0],
        "bootstrap_present": str("PYBEAM_BOOTSTRAP" in os.environ),
    }
    for name, operation in (("actors", actors), ("tasks", tasks), ("services", services)):
        try:
            results[name] = await operation()
        except Exception as error:
            results[name] = type(error).__name__ + ": " + str(error)
    channel = erlang.Channel(resource)
    try:
        message = await channel.async_receive()
        if message != "host-to-python":
            raise AssertionError(repr(message))
        erlang.send(host, (b"python-to-host", message))
        results["messages"] = "received host-to-python; sent reply to host pid"
    finally:
        channel.close()
    for name, operation in (("managed_guard", require_managed_entry), ("loader", load)):
        try:
            operation()
        except Exception as error:
            results[name] = type(error).__name__ + ": " + str(error)
        else:
            raise AssertionError(name + " unexpectedly succeeded")
    return results
