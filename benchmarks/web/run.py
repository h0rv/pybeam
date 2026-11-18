import argparse
import asyncio
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, PositiveInt

from benchmarks.metrics import percentile
from benchmarks.runner import measurement_lock
from benchmarks.web.ws import Command, Reply

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "pybeam-web:jammy"
PORT = 18764
type Variant = Literal["hornbeam", "managed", "uvicorn", "uvicorn2", "uvicorn-standard"]
VARIANTS: tuple[Variant, ...] = ("hornbeam", "managed", "uvicorn", "uvicorn2")
HOST_VARIANTS: tuple[Variant, ...] = ("hornbeam", "uvicorn", "uvicorn-standard")


@dataclass(frozen=True, slots=True)
class Settings:
    output: Path
    seconds: int
    repeats: int
    steps: tuple[int, ...]
    host_http: bool = False


def execute(args: list[str], *, timeout: float = 120) -> str:
    result = subprocess.run(  # noqa: S603
        args, check=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout.strip()


def required(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError("Missing executable: " + name + "; run inside mise exec")
    return path


def server_command(variant: Variant, app: str = "benchmarks.web.app:app") -> list[str]:
    python = str(ROOT / ".venv/bin/python")
    match variant:
        case "hornbeam":
            return [
                required("escript"),
                str(ROOT / "benchmarks/web/hornbeam.escript"),
                str(ROOT / "_build/hornbeam"),
                "127.0.0.1:" + str(PORT),
                str(ROOT),
                str(ROOT / ".venv/lib/python3.14/site-packages"),
                app,
            ]
        case "managed":
            return [python, str(ROOT / "benchmarks/web/managed.py"), "--app", app]
        case "uvicorn" | "uvicorn2" | "uvicorn-standard":
            workers = "2" if variant == "uvicorn2" else "1"
            standard = ["--standard"] if variant == "uvicorn-standard" else []
            return [
                python,
                "-m",
                "benchmarks.web.plain",
                "--workers",
                workers,
                *standard,
                "--app",
                app,
            ]


def container_command(
    variant: Variant, name: str, app: str = "benchmarks.web.app:app"
) -> list[str]:
    home = str(Path.home())
    path = str(Path(required("erl")).parent) + ":/usr/local/bin:/usr/bin:/bin"
    return [
        required("docker"),
        "run",
        "-d",
        "--name",
        name,
        "--cpus=2",
        "--cpuset-cpus=0-1",
        "--memory=2g",
        "--memory-swap=2g",
        "--network=host",
        "--ulimit",
        "nofile=200000:200000",
        "--mount",
        "type=bind,src=" + home + ",dst=" + home,
        "-w",
        str(ROOT / "benchmarks/web"),
        "-e",
        "PATH=" + path,
        "-e",
        "PYTHONPATH=" + str(ROOT) + ":" + str(ROOT / "src"),
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "WEB_PORT=" + str(PORT),
        IMAGE,
        *server_command(variant, app),
    ]


def wait_for_server(app: str, name: str, process: subprocess.Popen[bytes] | None = None) -> None:
    deadline = time.monotonic() + 30
    endpoint = "/" if app == "benchmarks.web.app:app" else "/ping"
    expected = b'{"message":"hello"}' if endpoint == "/" else b"ok"
    with httpx.Client(timeout=1) as client:
        while True:
            if process is not None and process.poll() is not None:
                raise RuntimeError("Server exited during startup; see " + name + ".log")
            try:
                response = client.get("http://127.0.0.1:" + str(PORT) + endpoint)
                response.raise_for_status()
                if response.content != expected:
                    raise RuntimeError("Unexpected HTTP response: " + response.text)
                return
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server startup timed out: " + name) from None
                time.sleep(0.1)


@contextmanager
def server(
    variant: Variant, name: str, output: Path, app: str = "benchmarks.web.app:app"
) -> Generator[None]:
    execute(container_command(variant, name, app))
    try:
        wait_for_server(app, name)
        yield
    finally:
        execute([required("docker"), "stop", "--time", "10", name], timeout=20)
        logs = subprocess.run(  # noqa: S603
            [required("docker"), "logs", name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
        (output / (name + ".log")).write_text(logs.stdout)
        (output / (name + ".state.json")).write_text(
            execute([required("docker"), "inspect", "--format", "{{json .State}}", name])
        )
        execute([required("docker"), "rm", name])


@contextmanager
def host_server(variant: Variant, name: str, output: Path) -> Generator[None]:
    app = "benchmarks.web.app:app"
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", PORT))
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT) + ":" + str(ROOT / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        WEB_PORT=str(PORT),
    )
    with (
        (output / (name + ".log")).open("wb") as log,
        subprocess.Popen(  # noqa: S603
            [required("taskset"), "-c", "0-1", *server_command(variant, app)],
            cwd=ROOT / "benchmarks/web",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        ) as process,
    ):
        try:
            wait_for_server(app, name, process)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


def http_load(
    path: Path, seconds: int, concurrency: int, rate: int | None = None, endpoint: str = "/"
) -> None:
    command = [
        required("taskset"),
        "-c",
        "4-7",
        required("oha"),
        "--no-tui",
        "--output-format",
        "json",
        "--output",
        str(path),
        "--worker-threads",
        "4",
        "--http-version",
        "1.1",
        "--disable-compression",
        "--latency-correction",
        "-w",
        "-t",
        "5s",
        "-z",
        str(seconds) + "s",
        "-c",
        str(concurrency),
    ]
    if rate is not None:
        command += ["-q", str(rate)]
    execute([*command, "http://127.0.0.1:" + str(PORT) + endpoint], timeout=seconds + 30)


class ContainerState(BaseModel):
    running: bool = Field(alias="Running")


def memory(name: str) -> int | None:
    try:
        return int(
            execute([required("docker"), "exec", name, "cat", "/sys/fs/cgroup/memory.current"])
        )
    except subprocess.CalledProcessError:
        deadline = time.monotonic() + 2
        while True:
            state = ContainerState.model_validate_json(
                execute([required("docker"), "inspect", "--format", "{{json .State}}", name])
            )
            if not state.running:
                return None
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


async def exchange(process: asyncio.subprocess.Process, command: Command) -> Reply:
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("Worker pipes are unavailable")
    process.stdin.write((command.model_dump_json() + "\n").encode())
    await process.stdin.drain()
    async with asyncio.timeout(120):
        line = await process.stdout.readline()
    if not line:
        raise RuntimeError("WebSocket load worker exited: " + str(process.returncode))
    return Reply.model_validate_json(line)


def memory_delta(baseline: int, peak: int | None, held: int) -> float | None:
    if peak is None or held == 0:
        return None
    return (peak - baseline) / held


async def broadcast(workers: list[asyncio.subprocess.Process], command: Command) -> list[Reply]:
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(exchange(worker, command)) for worker in workers]
    return [task.result() for task in tasks]


async def websocket_load(name: str, settings: Settings, path: Path) -> None:
    workers: list[asyncio.subprocess.Process] = []
    baseline = await asyncio.to_thread(memory, name)
    if baseline is None:
        raise RuntimeError("Server exited before WebSocket baseline")
    try:
        for index in range(4):
            workers.append(
                await asyncio.create_subprocess_exec(
                    required("taskset"),
                    "-c",
                    str(index + 4),
                    sys.executable,
                    "-m",
                    "benchmarks.web.ws",
                    "--url",
                    "ws://127.0.0.1:" + str(PORT) + "/ws",
                    "--source",
                    "127.0.0." + str(index + 2),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    cwd=ROOT,
                )
            )
        with path.open("w") as output:
            for target in settings.steps:
                started = time.monotonic()
                replies = await broadcast(workers, Command(action="open", target=target // 4))
                ramp_seconds = time.monotonic() - started
                errors = [error for reply in replies for error in reply.errors]
                held_start = sum(reply.held for reply in replies)
                latencies: list[float] = []
                measured_memory = await asyncio.to_thread(memory, name)
                peak = measured_memory
                hold_start = time.monotonic()
                deadline = hold_start + settings.seconds
                held_min = held_start
                while time.monotonic() < deadline:
                    samples = await broadcast(workers, Command(action="sample"))
                    held_min = min(held_min, sum(reply.held for reply in samples))
                    latencies.extend(value for reply in samples for value in reply.latency_ms)
                    errors.extend(error for reply in samples for error in reply.errors)
                    measured_memory = await asyncio.to_thread(memory, name)
                    if measured_memory is None:
                        errors.append("server container exited")
                        break
                    peak = measured_memory if peak is None else max(peak, measured_memory)
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
                final = await broadcast(workers, Command(action="sample"))
                held_min = min(held_min, sum(reply.held for reply in final))
                latencies.extend(value for reply in final for value in reply.latency_ms)
                errors.extend(error for reply in final for error in reply.errors)
                record = {
                    "target": target,
                    "held_start": held_start,
                    "held_min": held_min,
                    "hold_seconds": time.monotonic() - hold_start,
                    "ramp_seconds": ramp_seconds,
                    "baseline_bytes": baseline,
                    "memory_peak_bytes": peak,
                    "delta_bytes_per_connection": memory_delta(baseline, peak, held_min),
                    "echo_samples": len(latencies),
                    "echo_p99_ms": percentile(tuple(latencies), 0.99) if latencies else None,
                    "error_count": len(errors),
                    "first_errors": errors[:5],
                }
                output.write(json.dumps(record) + "\n")
                output.flush()
                print(name, "ws", json.dumps(record), flush=True)
                if held_min != target or errors:
                    break
    finally:
        async with asyncio.TaskGroup() as group:
            for process in workers:
                group.create_task(stop_worker(process))


async def stop_worker(process: asyncio.subprocess.Process) -> None:
    try:
        async with asyncio.timeout(30):
            if process.stdin is not None:
                try:
                    process.stdin.write(b'{"action":"close"}\n')
                    await process.stdin.drain()
                except BrokenPipeError, ConnectionResetError:
                    pass
            await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise RuntimeError("WebSocket worker failed: " + str(process.returncode))


class Summary(BaseModel):
    requests_per_second: float = Field(alias="requestsPerSec")


class HttpResult(BaseModel):
    summary: Summary


class CleanHttpResult(HttpResult):
    status_codes: dict[Literal["200"], PositiveInt] = Field(
        alias="statusCodeDistribution", min_length=1
    )
    errors: dict[str, int] = Field(alias="errorDistribution", max_length=0)


def run(settings: Settings) -> None:
    with measurement_lock():
        run_locked(settings)


def run_locked(settings: Settings) -> None:
    settings.output.mkdir(parents=True, exist_ok=False)
    variants = HOST_VARIANTS if settings.host_http else VARIANTS
    start_server = host_server if settings.host_http else server
    result_model = CleanHttpResult if settings.host_http else HttpResult
    rates: list[float] = []
    for repeat in range(settings.repeats):
        offset = repeat % len(variants) if settings.host_http else 0
        for variant in variants[offset:] + variants[:offset]:
            name = "web-" + variant + "-" + str(repeat)
            with start_server(variant, name, settings.output):
                warmup = settings.output / (name + ".warmup.json")
                http_load(warmup, 2, 16)
                for concurrency in (64, 256):
                    path = settings.output / (name + ".http" + str(concurrency) + ".json")
                    http_load(path, settings.seconds, concurrency)
                    result = result_model.model_validate_json(path.read_text())
                    rates.append(result.summary.requests_per_second)
                    print(name, "http", concurrency, result.summary.requests_per_second, flush=True)
    if settings.host_http:
        return
    fixed_rate = max(1, math.floor(min(rates) / 2))
    print("fixed_rate", fixed_rate, flush=True)
    for repeat in range(settings.repeats):
        for variant in VARIANTS:
            name = "web-fixed-" + variant + "-" + str(repeat)
            with server(variant, name, settings.output):
                path = settings.output / (name + ".json")
                http_load(path, settings.seconds, 64, fixed_rate)
                print(name, "fixed_rate", fixed_rate, flush=True)
            name = "web-ws-" + variant + "-" + str(repeat)
            with server(variant, name, settings.output):
                asyncio.run(websocket_load(name, settings, settings.output / (name + ".jsonl")))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--host-http", action="store_true", help="CPU-pinned HTTP only, no Docker")
    args = parser.parse_args()
    settings = Settings(
        output=Path(args.output).resolve(),
        seconds=2 if args.smoke else (10 if args.host_http else 30),
        repeats=1 if args.smoke else 3,
        steps=(40, 200) if args.smoke else (1000, 5000, 10000, 20000, 30000, 50000),
        host_http=args.host_http,
    )
    if settings.output.is_relative_to(ROOT):
        raise ValueError("Keep raw measurements outside the repository")
    run(settings)


if __name__ == "__main__":
    main()
