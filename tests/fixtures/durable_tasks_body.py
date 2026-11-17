import asyncio
from pathlib import Path

try:
    import erlang
except ModuleNotFoundError as error:
    if error.name != "erlang":
        raise
    erlang = None


async def complete(payload: bytes) -> bytes:
    await asyncio.sleep(0.2)
    await asyncio.to_thread(Path(payload.decode()).write_bytes, b"completed")
    return b""


async def cluster_complete(payload: bytes) -> bytes:
    if erlang is None:
        raise RuntimeError("Cluster body requires the managed runtime")
    await erlang.async_call("durable_body_started", payload)
    return await complete(payload)
