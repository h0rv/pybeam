import argparse
import asyncio
import errno
import json
import os
import resource
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path

import httpx
import psutil
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from benchmarks.runner import measurement_lock
from benchmarks.web.run import (
    HOST_VARIANTS,
    PORT,
    ROOT,
    Settings,
    Variant,
    broadcast,
    check_host_port,
    http_load,
    measure_websockets,
    read_reply,
    send_command,
    spawn_host_server,
    wait_for_server,
    websocket_workers,
)
from benchmarks.web.ws import Command, Reply

APP = "benchmarks.web.app:app"
HTTP_URL = "http://127.0.0.1:" + str(PORT) + "/"
WS_URL = "ws://127.0.0.1:" + str(PORT) + "/ws"
PROBE_INTERVAL = 0.05
RECOVERY_TIMEOUT = 30
scale_server = partial(spawn_host_server, extra_env={"GRANIAN_BACKPRESSURE": "65536"})


@dataclass(frozen=True, slots=True)
class Burst:
    held: int
    echoes: int
    errors: tuple[str, ...]

    @classmethod
    def from_replies(cls, replies: list[Reply]) -> Burst:
        return cls(
            held=sum(reply.held for reply in replies),
            echoes=sum(len(reply.latency_ms) for reply in replies),
            errors=tuple(error for reply in replies for error in reply.errors),
        )

    def clean(self, target: int) -> bool:
        return self.held == target and not self.errors


@dataclass(frozen=True, slots=True)
class Recovery:
    seconds: float | None
    failed_probes: int


@dataclass(frozen=True, slots=True)
class CrashResult:
    target: int
    opened: Burst
    before: Burst | None = None
    interrupted: Burst | None = None
    exit_code: int | None = None
    port_release: Recovery | None = None
    http: Recovery | None = None
    websocket: Recovery | None = None
    old_sockets_open: int | None = None
    reconnected: Burst | None = None
    full_recovery_seconds: float | None = None


def process_memory(pid: int) -> int | None:
    try:
        root = psutil.Process(pid)
        total = root.memory_full_info().pss
    except psutil.NoSuchProcess:
        return None
    for child in root.children(recursive=True):
        try:
            total += child.memory_full_info().pss
        except psutil.NoSuchProcess:
            continue
    return total


async def http_probe(client: httpx.AsyncClient) -> bool:
    try:
        response = await client.get(HTTP_URL)
        return response.status_code == 200 and response.content == b'{"message":"hello"}'
    except httpx.TransportError:
        return False


async def websocket_probe() -> bool:
    try:
        async with (
            asyncio.timeout(0.5),
            connect(WS_URL, compression=None, ping_interval=None, proxy=None) as socket,
        ):
            await socket.send("recovery")
            return await socket.recv() == "recovery"
    except OSError, TimeoutError, ConnectionClosed, InvalidHandshake:
        return False


async def wait_for_recovery(check: Callable[[], Awaitable[bool]], killed_at: float) -> Recovery:
    failures = 0
    while time.monotonic() - killed_at < RECOVERY_TIMEOUT:
        if await check():
            return Recovery(time.monotonic() - killed_at, failures)
        failures += 1
        # The servers don't share a readiness signal. Only a successful response counts.
        await asyncio.sleep(PROBE_INTERVAL)
    return Recovery(None, failures)


async def port_released() -> bool:
    try:
        check_host_port()
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        return False
    return True


async def crash_trial(variant: Variant, name: str, output: Path, target: int) -> CrashResult:
    async with (
        websocket_workers() as workers,
        httpx.AsyncClient(timeout=0.5, trust_env=False) as client,
    ):
        with scale_server(variant, name, output) as server:
            await asyncio.to_thread(wait_for_server, APP, name, server)
            open_connections = Command(action="open", target=target // len(workers))
            sample = Command(action="sample")
            opened = Burst.from_replies(await broadcast(workers, open_connections))
            if not opened.clean(target):
                return CrashResult(target, opened)
            before = Burst.from_replies(await broadcast(workers, sample))
            if not before.clean(target) or before.echoes == 0:
                return CrashResult(target, opened, before)

            async with asyncio.TaskGroup() as group:
                for worker in workers:
                    group.create_task(send_command(worker, sample))
            if server.poll() is not None:
                raise RuntimeError("Server exited before crash injection: " + name)
            killed_at = time.monotonic()
            os.killpg(server.pid, signal.SIGKILL)
            exit_code = await asyncio.to_thread(server.wait, 10)
            if exit_code != -signal.SIGKILL:
                raise RuntimeError("Server did not exit from SIGKILL: " + name)

        async def check_http() -> bool:
            return await http_probe(client)

        # A killed child can still be releasing its socket after the group leader exits.
        async with asyncio.TaskGroup() as group:
            port_task = group.create_task(wait_for_recovery(port_released, killed_at))
            burst_tasks = [group.create_task(read_reply(worker)) for worker in workers]
            port_release = await port_task
            if port_release.seconds is None:
                interrupted = Burst.from_replies([await task for task in burst_tasks])
                return CrashResult(
                    target, opened, before, interrupted, exit_code, port_release=port_release
                )
            with scale_server(variant, name + "-restart", output):
                http_task = group.create_task(wait_for_recovery(check_http, killed_at))
                ws_task = group.create_task(wait_for_recovery(websocket_probe, killed_at))
                http, websocket = await http_task, await ws_task
                interrupted = Burst.from_replies([await task for task in burst_tasks])
                old = Burst.from_replies(await broadcast(workers, Command(action="reset"))).held
                reconnected = None
                full_recovery = None
                if http.seconds is not None and websocket.seconds is not None:
                    reconnected = Burst.from_replies(await broadcast(workers, open_connections))
                    if reconnected.clean(target):
                        reconnected = Burst.from_replies(await broadcast(workers, sample))
                        if reconnected.clean(target) and reconnected.echoes == before.echoes:
                            full_recovery = time.monotonic() - killed_at
                return CrashResult(
                    target=target,
                    opened=opened,
                    before=before,
                    interrupted=interrupted,
                    exit_code=exit_code,
                    port_release=port_release,
                    http=http,
                    websocket=websocket,
                    old_sockets_open=old,
                    reconnected=reconnected,
                    full_recovery_seconds=full_recovery,
                )


def run(settings: Settings, http_steps: tuple[int, ...], crash_target: int) -> None:
    if settings.output.is_relative_to(ROOT):
        raise ValueError("Keep raw measurements outside the repository")
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if (
        soft_limit != resource.RLIM_INFINITY
        and soft_limit < max(*settings.steps, *http_steps) + 256
    ):
        raise ValueError("Raise the open-file limit (ulimit -n) before running the benchmark")
    with measurement_lock():
        settings.output.mkdir(parents=True, exist_ok=False)
        for repeat in range(settings.repeats):
            offset = repeat % len(HOST_VARIANTS)
            for variant in HOST_VARIANTS[offset:] + HOST_VARIANTS[:offset]:
                name = "scale-" + variant + "-" + str(repeat)
                with scale_server(variant, name + "-http", settings.output) as server:
                    wait_for_server(APP, name + "-http", server)
                    http_load(settings.output / (name + ".warmup.json"), 2, 16)
                    for concurrency in http_steps:
                        path = settings.output / (name + ".http" + str(concurrency) + ".json")
                        http_load(path, settings.seconds, concurrency)
                        print(name, "http", concurrency, flush=True)
                with scale_server(variant, name + "-ws", settings.output) as server:
                    wait_for_server(APP, name + "-ws", server)
                    asyncio.run(
                        measure_websockets(
                            name,
                            settings,
                            settings.output / (name + ".ws.jsonl"),
                            lambda: process_memory(server.pid),
                        )
                    )
                crash = asyncio.run(
                    crash_trial(variant, name + "-crash", settings.output, crash_target)
                )
                (settings.output / (name + ".crash.json")).write_text(
                    json.dumps(asdict(crash)) + "\n"
                )
                print(
                    name, "crash", "full recovery seconds", crash.full_recovery_seconds, flush=True
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="CPU-pinned high-concurrency and crash benchmark")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    os.sched_setaffinity(0, {4, 5, 6, 7})
    run(
        Settings(
            output=args.output.resolve(),
            seconds=2 if args.smoke else 10,
            repeats=1 if args.smoke else 3,
            steps=(40, 200) if args.smoke else (1000, 5000, 10000, 20000),
        ),
        http_steps=(64,) if args.smoke else (1000, 5000, 10000),
        crash_target=200 if args.smoke else 5000,
    )


if __name__ == "__main__":
    main()
