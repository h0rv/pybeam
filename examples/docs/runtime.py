import asyncio

import pybeam


async def uppercase(text: str) -> str:
    await asyncio.sleep(0)
    return text.upper()


async def main() -> None:
    async with asyncio.TaskGroup() as work:
        first = work.create_task(uppercase("first"))
        second = work.create_task(uppercase("second"))
    print(first.result(), second.result())


if __name__ == "__main__":
    pybeam.run(main)
