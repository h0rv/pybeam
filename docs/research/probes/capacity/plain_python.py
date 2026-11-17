# plain_python.py
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

COUNT = int(sys.argv[2])


def status() -> tuple[int, int]:
    fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    return int(fields["VmRSS"].split()[0]), int(fields["Threads"])


def threads() -> dict[str, object]:
    rss0, _ = status()
    release = threading.Event()
    started = time.perf_counter()
    pool = [threading.Thread(target=release.wait) for _ in range(COUNT)]
    try:
        for thread in pool:
            thread.start()
        spawn = time.perf_counter() - started
        rss, count = status()
    finally:
        release.set()
        for thread in pool:
            if thread.is_alive():
                thread.join()
    started = time.perf_counter()
    pool = [threading.Thread(target=time.sleep, args=(0.05,)) for _ in range(min(COUNT, 1000))]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    return {
        "kind": "threads",
        "count": COUNT,
        "spawn_s": round(spawn, 3),
        "rss_delta_kib": rss - rss0,
        "threads": count,
        "sleep_50ms_x1000_s": round(time.perf_counter() - started, 3),
    }


async def tasks() -> dict[str, object]:
    rss0, _ = status()
    release = asyncio.Event()
    started = time.perf_counter()
    pending = [asyncio.create_task(release.wait()) for _ in range(COUNT)]
    await asyncio.sleep(0)
    spawn = time.perf_counter() - started
    rss, count = status()
    release.set()
    await asyncio.gather(*pending)
    started = time.perf_counter()
    await asyncio.gather(*(asyncio.sleep(0.05) for _ in range(COUNT)))
    return {
        "kind": "asyncio_tasks",
        "count": COUNT,
        "spawn_s": round(spawn, 3),
        "rss_delta_kib": rss - rss0,
        "threads": count,
        "sleep_50ms_all_s": round(time.perf_counter() - started, 3),
    }


result = threads() if sys.argv[1] == "threads" else asyncio.run(tasks())
result["python"] = sys.version.split()[0] + ("t" if not sys._is_gil_enabled() else "")
print(json.dumps(result))
