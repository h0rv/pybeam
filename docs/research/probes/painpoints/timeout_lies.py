# timeout_lies.py
import asyncio
import threading
import time

effects: list[float] = []
done = threading.Event()


def charge_card() -> None:
    for _ in range(30):
        time.sleep(0.1)
        effects.append(time.monotonic())
    done.set()


async def main() -> None:
    try:
        await asyncio.wait_for(asyncio.to_thread(charge_card), timeout=0.5)
    except TimeoutError:
        timed_out = time.monotonic()
        print("timed out; caller believes the work stopped")
    else:
        raise RuntimeError("Blocking charge completed before the timeout")
    await asyncio.to_thread(done.wait)
    after = sum(1 for at in effects if at > timed_out)
    print("side effects after the timeout:", after, "of", len(effects))


if __name__ == "__main__":
    asyncio.run(main())
