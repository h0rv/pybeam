# genstage_transform.py
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Work(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    id: int
    action: Literal["ok"]
    text: str


def uppercase(payload: bytes) -> bytes:
    # Parse one request before using its fields.
    work = Work.model_validate_json(payload)
    return work.text.upper().encode("utf-8")
