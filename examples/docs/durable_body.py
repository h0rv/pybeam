# durable_body.py
import asyncio
from pathlib import Path


async def write_report(payload: bytes) -> bytes:
    await asyncio.to_thread(Path("report.txt").write_bytes, payload)
    return b""
