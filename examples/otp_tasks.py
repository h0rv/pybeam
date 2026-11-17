import pybeam
from pybeam.codec import Json
from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor


async def twice(value: int) -> int:
    return value * 2


async def main() -> None:
    async with TaskSupervisor(
        TaskLimits(capacity=2, max_bytes=4096, shutdown_ms=1000)
    ) as supervisor:
        task = await supervisor.start(TaskFunction(twice, Json(int), Json(int)), 21)
        print("OTP task PID:", task.pid)
        print("OTP task result:", await task)


pybeam.run(main)
