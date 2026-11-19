import os
from collections.abc import AsyncIterator

from pydantic import TypeAdapter
from redis.asyncio import Redis

from examples.agents.protocol import Execution
from examples.agents.usual.models import Turn, View
from examples.agents.views import templates

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def key(session: str) -> str:
    return "agents:session:" + session


def topic(session: str) -> str:
    return "agents:events:" + session


def transcript(turn: Turn) -> str:
    return templates.get_template("transcript.html").render(snapshot=turn, usual=True)


async def save(kv: Redis, turn: Turn) -> None:
    async with kv.pipeline(transaction=True) as pipe:
        pipe.set(key(turn.id), turn.model_dump_json())
        pipe.publish(topic(turn.id), transcript(turn))
        await pipe.execute()


async def read(kv: Redis, session: str) -> Turn | None:
    value = await kv.get(key(session))
    return Turn.model_validate_json(value) if value is not None else None


async def all_turns(kv: Redis) -> list[Turn]:
    ids = TypeAdapter(list[bytes]).validate_python(
        await kv.execute_command("LRANGE", "agents:sessions", 0, -1)
    )
    if not ids:
        return []
    values = await kv.mget([key(value.decode()) for value in ids])
    return [Turn.model_validate_json(value) for value in values if value is not None]


async def view(kv: Redis, turn: Turn) -> View:
    entries = TypeAdapter(list[bytes]).validate_python(
        await kv.execute_command("LRANGE", "agents:executions:" + turn.id, 0, -1)
    )
    executions: dict[str, list[Execution]] = {}
    for entry in entries:
        call_id, record = entry.split(b" ", 1)
        executions.setdefault(call_id.decode(), []).append(Execution.model_validate_json(record))
    return View(snapshot=turn, executions=executions)


async def fragments(kv: Redis, session: str) -> AsyncIterator[str]:
    async with kv.pubsub() as subscription:
        await subscription.subscribe(topic(session))
        turn = await read(kv, session)
        if turn is not None:
            yield transcript(turn)
        async for message in subscription.listen():
            data = message["data"]
            if message["type"] == "message" and isinstance(data, bytes):
                yield data.decode()
