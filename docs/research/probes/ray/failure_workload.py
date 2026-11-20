import os
import time
from pathlib import Path


def wait_file(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Actor did not enter the call")
        time.sleep(0.01)


def mark(directory: Path, task_id: int) -> None:
    with (directory / str(task_id)).open("ab", buffering=0) as output:
        output.write(b"done\n")
        os.fsync(output.fileno())


def counts(directory: Path) -> dict[str, int]:
    deliveries = [
        len((directory / str(task_id)).read_bytes().splitlines())
        if (directory / str(task_id)).exists()
        else 0
        for task_id in range(20)
    ]
    return {
        "once": deliveries.count(1),
        "twice": deliveries.count(2),
        "more_than_twice": sum(value > 2 for value in deliveries),
        "missing": deliveries.count(0),
        "total_writes": sum(deliveries),
    }
