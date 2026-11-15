import asyncio
from time import perf_counter_ns
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PAYLOAD_LIMIT = 1024


class Work(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: Annotated[int, Field(ge=0)]
    action: Literal["ok", "slow", "fail", "cancel"]
    text: str = ""


class Outcome(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: int
    status: Literal["ok", "failed", "cancelled"]
    detail: str
    python_ns: Annotated[int, Field(ge=0)]


async def handle(work: Work) -> str:
    match work.action:
        case "ok":
            return work.text.upper()
        case "slow":
            await asyncio.sleep(0.05)
            return work.text.upper()
        case "fail":
            raise ValueError("deliberate callback failure")
        case "cancel":
            await asyncio.sleep(10)
            return work.text.upper()


async def execute(work: Work) -> Outcome:
    started = perf_counter_ns()
    try:
        async with asyncio.timeout(0.2 if work.action == "slow" else 0.02):
            detail = await handle(work)
        status: Literal["ok", "failed", "cancelled"] = "ok"
    except TimeoutError:
        status, detail = "cancelled", "application deadline expired"
    except ValueError as error:
        status, detail = "failed", str(error)
    return Outcome(
        id=work.id,
        status=status,
        detail=detail,
        python_ns=perf_counter_ns() - started,
    )


def process(payload: bytes) -> bytes:
    """Parse only at the native boundary; deadlines are cooperative, not isolation."""
    if len(payload) > PAYLOAD_LIMIT:
        raise ValueError("callback payload exceeds 1024 bytes")
    work = Work.model_validate_json(payload)
    result = asyncio.run(execute(work)).model_dump_json().encode("utf-8")
    if len(result) > PAYLOAD_LIMIT:
        raise ValueError("callback result exceeds 1024 bytes")
    return result


def identity(payload: bytes) -> bytes:
    return payload
