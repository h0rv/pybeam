# app.py
import asyncio

import pybeam


async def main() -> None:
    print("Serving until stopped", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    pybeam.run(main)
