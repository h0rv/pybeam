import asyncio
from dataclasses import dataclass
from weakref import ReferenceType


@dataclass(frozen=True, slots=True)
class Snapshot:
    value: int
    label: str


@dataclass(slots=True, weakref_slot=True)
class Counter:
    value: int = 0
    label: str = ""

    async def add(self, amount: int, /, *, label: str) -> int:
        old = self.value
        await asyncio.sleep(0.001)
        self.value = old + amount
        self.label = label
        return self.value

    async def read(self) -> Snapshot:
        return Snapshot(self.value, self.label)


def show_results(results: tuple[int, ...], first: Snapshot, second: Snapshot) -> None:
    ordered = tuple(sorted(results))
    if ordered != tuple(range(1, 9)):
        raise AssertionError("Concurrent increments were not serialized")
    if first != Snapshot(8, "visits") or second != Snapshot(2, "other"):
        raise AssertionError("Actor state was not independent")
    print("Concurrent results: {}".format(ordered), flush=True)
    print("First actor: value={}, label={}".format(first.value, first.label), flush=True)
    print("Second actor: value={}, label={}".format(second.value, second.label), flush=True)


def verify_collected(first: ReferenceType[Counter], second: ReferenceType[Counter]) -> None:
    if first() is not None or second() is not None:
        raise AssertionError("Stopped actor state is still retained")
