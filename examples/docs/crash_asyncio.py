# crash_asyncio.py
import asyncio
import ctypes


async def crash(payload: bytes) -> bytes:
    ctypes.string_at(0)
    return payload


async def main() -> None:
    print("Calling native code", flush=True)
    try:
        await crash(b"")
    except Exception as error:
        print("Call raised:", type(error).__name__)
    print("Still running:", 6 * 7)


if __name__ == "__main__":
    asyncio.run(main())
