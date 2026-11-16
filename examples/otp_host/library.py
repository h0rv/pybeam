import statistics
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

import pybeam

type Sample = Annotated[float, Field(ge=-1e100, le=1e100)]


class Batch(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)

    values: Annotated[tuple[Sample, ...], Field(min_length=1, max_length=512)]


class Summary(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)

    count: int
    mean: float
    deviation: float
    minimum: float
    maximum: float


def summarize(batch: Batch) -> Summary:
    return Summary(
        count=len(batch.values),
        mean=statistics.fmean(batch.values),
        deviation=statistics.pstdev(batch.values),
        minimum=min(batch.values),
        maximum=max(batch.values),
    )


@pybeam.actor
class Analyzer:
    async def summarize(self, batch: Batch) -> Summary:
        return summarize(batch)


async def analyze(payload: str | bytes) -> str:
    if len(payload) > 65_536:
        raise ValueError("Batch JSON exceeds 65536 bytes")
    encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(encoded) > 65_536:
        raise ValueError("Batch JSON exceeds 65536 bytes")
    batch = Batch.model_validate_json(encoded)
    async with pybeam.ActorGroup(max_children=1, max_pending=4, max_bytes=65_536) as group:
        analyzer = await group.spawn(Analyzer)
        result = await analyzer.summarize(batch)
    return result.model_dump_json()


async def main() -> None:
    print(await analyze('{"values":[2.0,4.0,6.0,8.0]}'))


if __name__ == "__main__":
    pybeam.run(main)
