# crash_pool.py
import ctypes
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool


def crash() -> bytes:
    time.sleep(0.1)
    ctypes.string_at(0)
    return b""


def main() -> None:
    with ProcessPoolExecutor(max_workers=1) as pool:
        failed = pool.submit(crash)
        pending = pool.submit(bytes, b"queued")
        calls = (("Crashing call", failed), ("Pending call", pending))
        for label, future in calls:
            try:
                future.result()
            except BrokenProcessPool:
                print(label + ": BrokenProcessPool")
        try:
            pool.submit(bytes, b"later")
        except BrokenProcessPool:
            print("Later call: BrokenProcessPool")
    print("Caller still running")


if __name__ == "__main__":
    main()
