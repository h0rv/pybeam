import json
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
PROBE = ROOT / "examples/modes_lab/probe.escript"
JOBS = {"cpu": (ROOT / "examples/modes_lab", 3_000_000), "block": (HERE / "blocking", 50_000)}
MODES = ("worker", "owngil", "isolated")
COUNTS = tuple(int(n) for n in os.environ.get("PROBE_COUNTS", "1,8,64").split(","))
FANIN = tuple(int(n) for n in os.environ.get("PROBE_FANIN", "10000,100000,1000000").split(","))


def probe(app: str, job: Path, kind: str, mode: str, count: int, size: int) -> dict[str, object]:
    escript = shutil.which("escript") or "escript"
    arguments = [escript, str(PROBE), app, str(job), kind, mode, str(count), str(size)]
    done = subprocess.run(arguments, capture_output=True, text=True, timeout=600, check=False)  # noqa: S603
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if not lines:
        return {"error": "exit " + str(done.returncode)}
    parsed: object = json.loads(lines[-1])
    if not isinstance(parsed, dict):
        raise TypeError("Expected a probe result", parsed)
    return dict[str, object](parsed)


def number(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError("Expected an integer", value)
    return value


def items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("Expected a list", value)
    return list[object](value)


def field(span: object, key: str) -> int:
    if not isinstance(span, dict):
        raise TypeError("Expected a span", span)
    return number(span[key])


def summary(result: dict[str, object]) -> dict[str, object]:
    if "error" in result:
        return {"error": result["error"]}
    created = [number(value) for value in items(result["create_us"])]
    spans = items(result["spans"])
    return {
        "create_ms": round(statistics.median(created) / 1000, 1),
        "rss_mib_per_context": round(number(result["rss_kib"]) / len(created) / 1024, 1),
        "call_p50_us": result["latency_p50_us"],
        "single_s": round(number(result["single_ns"]) / 1e9, 3),
        "wall_s": round(number(result["wall_us"]) / 1e6, 3),
        "threads": len({field(span, "thread") for span in spans}),
        "processes": len({field(span, "pid") for span in spans}),
    }


def main() -> None:
    app = sys.argv[1]
    for name, (job, size) in JOBS.items():
        for mode in MODES:
            for count in COUNTS:
                row = summary(probe(app, job, "fanout", mode, count, size))
                print(json.dumps({"job": name, "mode": mode, "count": count, **row}), flush=True)
    for mode in MODES:
        for count in FANIN:
            fanin = probe(app, ROOT / "examples/modes_lab", "fanin", mode, count, 8)
            keys = ("spawn_us", "bytes_per_process", "wall_us", "rss_kib", "error")
            row = {k: fanin[k] for k in keys if k in fanin}
            print(json.dumps({"job": "fanin", "mode": mode, "count": count, **row}), flush=True)
    for mode in MODES:
        crash = probe(app, ROOT / "examples/modes_lab", "crash", mode, 4, 0)
        keys = ("others_alive", "victim_result", "replacement_us", "error")
        print(
            json.dumps({"job": "crash", "mode": mode, **{k: crash[k] for k in keys if k in crash}})
        )


if __name__ == "__main__":
    main()
