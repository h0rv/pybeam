import asyncio

import pybeam


async def main() -> None:
    print("MANAGED_READY", flush=True)
    await asyncio.sleep(300)


if __name__ == "__main__":
    pybeam.run(main)
