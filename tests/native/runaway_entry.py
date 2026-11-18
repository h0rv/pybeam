from pathlib import Path

from pybeam.codec import Json


async def runaway(payload: bytes) -> bytes:
    mark_entered(payload)
    value = 0
    while True:
        value += 1


def mark_entered(payload: bytes) -> None:
    Path(Json(str).decode(payload)).touch()
