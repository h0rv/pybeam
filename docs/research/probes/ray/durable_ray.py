import contextlib
import sys
import time
from pathlib import Path

import ray
from failure_workload import mark


def complete(task_id: int) -> None:
    time.sleep(0.5)
    mark(Path("markers"), task_id)


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        try:
            ray.init(
                num_cpus=1,
                include_dashboard=False,
                log_to_driver=False,
                object_store_memory=256 * 2**20,
            )
            work = ray.remote(num_cpus=1, max_retries=0)(complete)
            pending: list[ray.ObjectRef[None]] = []
            if sys.argv[1] == "submit":
                pending = [work.remote(task_id) for task_id in range(20)]
                Path("submitted").touch()
            Path("ready").touch()
            time.sleep(110)
        finally:
            ray.shutdown()
