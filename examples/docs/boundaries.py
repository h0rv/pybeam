import asyncio

import pybeam


async def main() -> None:
    changes: list[str] = []
    try:
        async with asyncio.timeout(0.01):
            changes.append("sent")
            await asyncio.sleep(1)
    except TimeoutError:
        print(changes)


if __name__ == "__main__":
    pybeam.run(main)
