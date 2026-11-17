import statistics
from dataclasses import dataclass

import pybeam
from pybeam.codec import Json


@dataclass(frozen=True, slots=True, kw_only=True)
class Batch:
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.values) <= 512:
            raise ValueError("values must have 1 to 512 samples")
        if any(not -1e100 <= value <= 1e100 for value in self.values):
            raise ValueError("values must be between -1e100 and 1e100")


@dataclass(frozen=True, slots=True, kw_only=True)
class Summary:
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
    batch = Json(Batch).decode(encoded)
    async with pybeam.ActorGroup(max_children=1, max_pending=4, max_bytes=65_536) as group:
        analyzer = await group.spawn(Analyzer)
        result = await analyzer.summarize(batch)
    return Json(type(result)).encode(result).decode()


async def main() -> None:
    print(await analyze('{"values":[2.0,4.0,6.0,8.0]}'))


if __name__ == "__main__":
    pybeam.run(main)
