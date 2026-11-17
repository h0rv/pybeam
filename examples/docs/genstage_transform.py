from dataclasses import dataclass
from typing import Literal

from pybeam.codec import Json


@dataclass(frozen=True, slots=True, kw_only=True)
class Work:
    id: int
    action: Literal["ok"]
    text: str


def uppercase(payload: bytes) -> bytes:
    # Parse one request before using its fields.
    work = Json(Work).decode(payload)
    return work.text.upper().encode("utf-8")
