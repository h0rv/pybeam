from genstage_callbacks import Outcome, process

import pybeam
from pybeam.codec import Json
from pybeam.integrations.genstage.pipeline import stream


async def main() -> None:
    async with stream(16, process) as outcomes:
        received = [Json(Outcome).decode(payload) async for payload in outcomes]
    if [item.id for item in received] != list(range(16)) or not all(
        item.detail == "HÉLLO" for item in received
    ):
        raise RuntimeError("GenStage returned an incorrect transform or order")
    print("GenStage transformed 16 ordered items in 16 single-credit batches")


pybeam.run(main)
