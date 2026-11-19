import os
import sys
import time
from pathlib import Path

from celery import Celery
from failure_workload import mark

app = Celery("failure_probe", broker=os.environ["PROBE_REDIS_URL"])
app.conf.update(
    task_default_queue=os.environ["PROBE_QUEUE"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={
        "visibility_timeout": 2,
        "unacked_restore_interval": 1,
        "unacked_restore_throttle": 1,
    },
)


def complete(task_id: int) -> None:
    time.sleep(0.5)
    mark(Path("markers"), task_id)


app.task(name="probe.complete")(complete)


if __name__ == "__main__" and sys.argv[1] == "submit":
    for task_id in range(20):
        app.send_task("probe.complete", args=[task_id])
    Path("submitted").touch()
