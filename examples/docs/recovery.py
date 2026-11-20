# recovery.py
import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def increment(self) -> int:
        self.value += 1
        return self.value


async def main() -> None:
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter)
        print("Before crash:", await counter.increment())
        await actors.crash(counter)
        replacement = await actors.rejoin(counter)
        print("After restart:", await replacement.increment())
    print("Caller keeps running")


if __name__ == "__main__":
    pybeam.run(main)
