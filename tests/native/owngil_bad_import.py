from pydantic import BaseModel


class Request(BaseModel):
    value: int


async def echo(payload: bytes) -> bytes:
    return payload
