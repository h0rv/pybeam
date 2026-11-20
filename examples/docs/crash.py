# crash.py
import ctypes

import crash as entries

import pybeam


async def crash(payload: bytes) -> bytes:
    ctypes.string_at(0)
    return payload


async def main() -> None:
    try:
        await pybeam.isolated(entries.crash, b"")
    except pybeam.TaskCrashed as error:
        print("Call crashed:", error.reason)
    print("Still running:", 6 * 7)


if __name__ == "__main__":
    pybeam.run(main)
