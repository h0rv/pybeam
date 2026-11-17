from erlang.tasks import entrypoint
from pydantic import BaseModel


class Request(BaseModel):
    value: int


@entrypoint
async def echo(payload: bytes) -> bytes:
    return payload
