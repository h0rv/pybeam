# execution.py
from pybeam import run
from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskFunction, TaskLimits, TaskSupervisor


async def checksum(iterations: int) -> int:
    value = 0
    for index in range(iterations):
        value = (value * 31 + index) & 0xFFFFFFFF
    return value


async def main() -> None:
    job = TaskFunction(
        checksum,
        Json(int),
        Json(int),
        execution=ExecutionMode.WORKER,
        dedicated_loop=True,
    )
    limits = TaskLimits(capacity=4, max_bytes=4096, shutdown_ms=1000)

    async with TaskSupervisor(limits) as supervisor:
        tasks = [await supervisor.start(job, 3_000_000) for _ in range(4)]

        for task in tasks:
            print(await task)


if __name__ == "__main__":
    run(main)
