import os

from celery import Celery
from redis import Redis

from examples.chat_load.jobs import checksum
from examples.chat_load.protocol import JobDone

queue = Celery("chat", broker=os.environ.get("BROKER_URL", "amqp://guest:guest@broker//"))
queue.conf.task_ignore_result = True
kv = Redis.from_url(os.environ.get("KV_URL", "redis://kv:6379/0"))


CHECKSUM = "chat.checksum"


def checksum_job(job: str, room: str, size: int) -> None:
    done = JobDone(job=job, result=checksum(size))
    kv.incr("chat-jobs:" + room)
    kv.publish("chat:" + room, done.model_dump_json())


queue.task(name=CHECKSUM)(checksum_job)
