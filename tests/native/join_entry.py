import asyncio
import os
from pathlib import Path

from pybeam import run


async def main() -> None:
    await asyncio.to_thread(Path(os.environ["JOIN_READY"]).touch)
    await asyncio.Event().wait()


run(main)
