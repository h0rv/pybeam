import asyncio
from dataclasses import dataclass

from pybeam import run
from pybeam.actors import ActorSpec, UnboundedMailbox, spawn


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


async def main() -> None:
    spec = ActorSpec(Counter, methods=(Counter.add, Counter.read))
    # Finite native actor mailbox admission is not available yet.
    async with (
        await spawn(spec, mailbox=UnboundedMailbox()) as first,
        await spawn(spec, mailbox=UnboundedMailbox()) as second,
    ):
        async with asyncio.TaskGroup() as tasks:
            increments = [
                tasks.create_task(first.call(Counter.add, 1, label="visits")) for _ in range(8)
            ]
            tasks.create_task(second.call(Counter.add, 2, label="other"))
        show_results(
            tuple(task.result() for task in increments),
            await first.call(Counter.read),
            await second.call(Counter.read),
        )


if __name__ == "__main__":
    run(main)
