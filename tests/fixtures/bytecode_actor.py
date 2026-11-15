import json
import sys

import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, amount: int) -> int:
        self.value += amount
        return self.value

    async def read(self) -> int:
        return self.value


async def main() -> None:
    assert sys.dont_write_bytecode
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter, value=10)
        value = await counter.add(2)
        snapshot = await counter.read()
        assert value == snapshot == 12
        print(
            "IMMUTABLE_BUNDLE "
            + json.dumps(
                {
                    "dont_write_bytecode": sys.dont_write_bytecode,
                    "value": value,
                    "snapshot": snapshot,
                }
            )
        )


if __name__ == "__main__":
    pybeam.run(main)
