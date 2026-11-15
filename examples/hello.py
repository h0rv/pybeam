import asyncio

import pybeam


async def main() -> None:
    await asyncio.sleep(0.01)
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
