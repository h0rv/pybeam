import asyncio
import os

from pydantic import BaseModel, TypeAdapter
from pydantic import __version__ as pydantic_version
from pydantic_core import __version__ as core_version

import pybeam


class Value(BaseModel):
    value: int


async def double(payload: bytes) -> bytes:
    request = Value.model_validate_json(payload)
    return Value(value=request.value * 2).model_dump_json().encode()


async def imports(payload: bytes) -> bytes:
    return TypeAdapter(dict[str, str | int]).dump_json(
        {
            "pydantic": pydantic_version,
            "pydantic_core": core_version,
            "pybeam": pybeam.__name__,
            "pid": os.getpid(),
        }
    )


async def crash(payload: bytes) -> bytes:
    await asyncio.sleep(0.01)
    os.abort()
