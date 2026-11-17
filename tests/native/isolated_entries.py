import asyncio
import os

from pydantic import __version__ as pydantic_version
from pydantic_core import __version__ as core_version

import pybeam
from pybeam.codec import Json


async def double(payload: bytes) -> bytes:
    request = Json(dict[str, int]).decode(payload)
    return Json(dict[str, int]).encode({"value": request["value"] * 2})


async def imports(payload: bytes) -> bytes:
    return Json(dict[str, str | int]).encode(
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
