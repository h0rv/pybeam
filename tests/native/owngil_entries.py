import asyncio
import json
import sys
import time
from pathlib import Path


async def double(payload: bytes) -> bytes:
    request: object = json.loads(payload)
    if not isinstance(request, dict) or not isinstance(value := request.get("value"), int):
        raise ValueError("Expected an integer value")
    return json.dumps({"value": value * 2}).encode()


async def echo(payload: bytes) -> bytes:
    return payload


async def empty_result(payload: bytes) -> bytes:
    return b""


async def failure(payload: bytes) -> bytes:
    raise ValueError("remote entry failed")


async def waiting(payload: bytes) -> bytes:
    path: object = json.loads(payload)
    if not isinstance(path, str):
        raise ValueError("Expected a path")
    marker = Path(path)
    try:
        await asyncio.to_thread(marker.write_text, "running")
        await asyncio.sleep(60)
    finally:
        await asyncio.to_thread(marker.write_text, "finished")
    return b"null"


async def cpu(payload: bytes) -> bytes:
    deadline: object = json.loads(payload)
    if not isinstance(deadline, int):
        raise ValueError("Expected a monotonic deadline")
    await asyncio.sleep(max(0, (deadline - time.monotonic_ns()) / 1_000_000_000))
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1)
    try:
        begin = time.monotonic_ns()
        value = 1
        while time.monotonic_ns() - begin < 400_000_000:
            value = (value * 13 + 17) % 100_003
        end = time.monotonic_ns()
    finally:
        sys.setswitchinterval(interval)
    assert value >= 0
    return json.dumps([begin, end]).encode()
