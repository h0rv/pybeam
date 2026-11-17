import asyncio
import errno
import resource
import socket
import subprocess
import sys
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from uvicorn import Config, Server
from websockets.asyncio.client import ClientConnection

from benchmarks.runner import measurement_lock
from benchmarks.web.app import app
from benchmarks.web.run import (
    Settings,
    Variant,
    container_command,
    memory,
    memory_delta,
    run,
    stop_worker,
)
from benchmarks.web.ws import Command, Pool, ensure_client_fds


@pytest_asyncio.fixture(loop_scope="function")
async def web_url() -> AsyncGenerator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(128)
        listener.setblocking(False)
        server = Server(Config(app, log_level="error", lifespan="off", ws="websockets"))
        async with asyncio.TaskGroup() as group:
            task = group.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:  # noqa: ASYNC110
                        await asyncio.sleep(0.01)
                yield "127.0.0.1:" + str(port)
            finally:
                server.should_exit = True
                await task


@pytest.mark.asyncio
async def test_http_and_echo(web_url: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.get("http://" + web_url + "/")
        assert response.status_code == 200
        assert response.content == b'{"message":"hello"}'
    pool = Pool("ws://" + web_url + "/ws", "127.0.0.2")
    try:
        opened = await pool.ramp(4)
        assert opened.held == 4
        assert not opened.errors
        first = pool.sockets[0]
        for message in ("", "snow-雪"):
            await first.send(message)
            assert await first.recv() == message
        sample = await pool.sample()
        assert sample.held == 4
        assert len(sample.latency_ms) == 4
        assert not sample.errors
    finally:
        await pool.close()
    assert pool.held() == 0


@pytest.mark.asyncio
async def test_empty_pool() -> None:
    reply = await Pool("ws://127.0.0.1:1/ws", "127.0.0.2").sample()
    assert reply.held == 0
    assert not reply.latency_ms
    assert not reply.errors


@pytest.mark.parametrize(
    ("soft", "hard", "target", "raised"),
    [
        (1024, 200000, 12500, True),
        (1024, resource.RLIM_INFINITY, 12500, True),
        (65536, 200000, 12500, False),
        (resource.RLIM_INFINITY, resource.RLIM_INFINITY, 12500, False),
        (32, 32, 0, False),
    ],
)
def test_client_fd_budget(
    monkeypatch: pytest.MonkeyPatch, soft: int, hard: int, target: int, raised: bool
) -> None:
    changes: list[tuple[int, int]] = []
    monkeypatch.setattr("benchmarks.web.ws.resource.getrlimit", lambda limit: (soft, hard))
    monkeypatch.setattr(
        "benchmarks.web.ws.resource.setrlimit", lambda limit, limits: changes.append(limits)
    )
    ensure_client_fds(target)
    assert changes == ([(target + 64, hard)] if raised else [])


@pytest.mark.asyncio
async def test_insufficient_client_fds_abort_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("benchmarks.web.ws.resource.getrlimit", lambda limit: (1024, 1024))
    with pytest.raises(RuntimeError, match="Client-side file descriptor limit cannot cover"):
        await Pool("ws://127.0.0.1:1/ws", "127.0.0.2").ramp(5000)


def test_client_fd_raise_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(limit: int, limits: tuple[int, int]) -> None:
        raise PermissionError("limit denied")

    monkeypatch.setattr("benchmarks.web.ws.resource.getrlimit", lambda limit: (1024, 200000))
    monkeypatch.setattr("benchmarks.web.ws.resource.setrlimit", denied)
    with pytest.raises(RuntimeError, match="Client-side file descriptor limit could not be raised"):
        ensure_client_fds(5000)


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [errno.EMFILE, errno.ENFILE])
async def test_client_fd_exhaustion_is_not_a_server_error(
    monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    async def exhausted(*args: object, **kwargs: object) -> ClientConnection:
        raise OSError(number, "file descriptor exhaustion")

    monkeypatch.setattr("benchmarks.web.ws.connect", exhausted)
    errors: list[str] = []
    with pytest.raises(RuntimeError, match="Client-side file descriptor exhaustion"):
        await Pool("ws://127.0.0.1:1/ws", "127.0.0.2").open_one(errors)
    assert errors == []


@pytest.mark.asyncio
async def test_refused_connections_are_not_counted() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        pool = Pool("ws://127.0.0.1:" + str(port) + "/ws", "127.0.0.2")
        reply = await pool.ramp(1)
        assert reply.held == 0
        assert reply.errors


def test_commands_reject_invalid_targets() -> None:
    for text in ('{"action":"open","target":-1}', '{"action":"unknown"}'):
        with pytest.raises(ValidationError):
            Command.model_validate_json(text)


def test_containers_have_identical_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("benchmarks.web.run.required", lambda name: "/usr/bin/" + name)
    for variant in ("hornbeam", "managed", "uvicorn", "uvicorn2"):
        command = container_command(variant, "test-web")
        for flag in ("--cpus=2", "--cpuset-cpus=0-1", "--memory=2g", "--memory-swap=2g"):
            assert flag in command


@pytest.mark.parametrize(
    ("peak", "held", "expected"),
    [(250, 3, 50.0), (250, 0, None), (None, 3, None), (None, 0, None)],
)
def test_memory_delta(peak: int | None, held: int, expected: float | None) -> None:
    assert memory_delta(100, peak, held) == expected


def test_memory_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("benchmarks.web.run.required", lambda name: name)
    monkeypatch.setattr("benchmarks.web.run.execute", lambda args: "1234")
    assert memory("server") == 1234


@pytest.mark.parametrize("running", [False, True])
def test_memory_during_exit(monkeypatch: pytest.MonkeyPatch, running: bool) -> None:
    states = iter([running, False])

    def execute(args: list[str]) -> str:
        if "exec" in args:
            raise subprocess.CalledProcessError(127, args)
        return '{"Running":' + str(next(states)).lower() + "}"

    monkeypatch.setattr("benchmarks.web.run.required", lambda name: name)
    monkeypatch.setattr("benchmarks.web.run.execute", execute)
    monkeypatch.setattr("benchmarks.web.run.time.sleep", lambda seconds: None)
    assert memory("server") is None


def test_memory_preserves_running_container_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0, 3])

    def execute(args: list[str]) -> str:
        if "exec" in args:
            raise subprocess.CalledProcessError(127, args)
        return '{"Running":true}'

    monkeypatch.setattr("benchmarks.web.run.required", lambda name: name)
    monkeypatch.setattr("benchmarks.web.run.execute", execute)
    monkeypatch.setattr("benchmarks.web.run.time.monotonic", lambda: next(clock))
    with pytest.raises(subprocess.CalledProcessError):
        memory("server")


@pytest.mark.asyncio
async def test_worker_failure_is_reaped() -> None:
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "raise SystemExit(3)")
    with pytest.raises(RuntimeError, match="WebSocket worker failed: 3"):
        await stop_worker(process)
    assert process.returncode == 3


@pytest.mark.asyncio
async def test_worker_cancellation_is_reaped() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "input()", stdin=asyncio.subprocess.PIPE
    )
    task = asyncio.create_task(stop_worker(process))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("fail", [False, True])
def test_web_measurements_hold_shared_load_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: bool
) -> None:
    monkeypatch.setattr("benchmarks.runner.LOAD_LOCK", tmp_path / "load.lock")
    measurements: list[str] = []

    def assert_locked() -> None:
        with (
            pytest.raises(RuntimeError, match="Another benchmark owns the shared load lock"),
            measurement_lock(),
        ):
            pytest.fail("timed measurement did not hold the shared load lock")

    @contextmanager
    def server(variant: Variant, name: str, output: Path) -> Generator[None]:
        assert_locked()
        try:
            yield
        finally:
            assert_locked()

    def http_load(path: Path, seconds: int, concurrency: int, rate: int | None = None) -> None:
        assert_locked()
        if fail:
            raise RuntimeError("load failed")
        measurements.append(path.name)
        path.write_text('{"summary":{"requestsPerSec":100}}')

    async def websocket_load(name: str, settings: Settings, path: Path) -> None:
        assert_locked()
        measurements.append(name)

    monkeypatch.setattr("benchmarks.web.run.server", server)
    monkeypatch.setattr("benchmarks.web.run.http_load", http_load)
    monkeypatch.setattr("benchmarks.web.run.websocket_load", websocket_load)
    settings = Settings(tmp_path / "results", 30, 1, (1000, 5000))
    if fail:
        with pytest.raises(RuntimeError, match="load failed"):
            run(settings)
    else:
        run(settings)
        assert len(measurements) == 20
        assert sum(name.startswith("web-fixed-") for name in measurements) == 4
        assert sum(name.startswith("web-ws-") for name in measurements) == 4
    with measurement_lock():
        pass


def test_busy_load_lock_prevents_web_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("benchmarks.runner.LOAD_LOCK", tmp_path / "load.lock")
    settings = Settings(tmp_path / "results", 30, 1, (1000,))
    with (
        measurement_lock(),
        pytest.raises(RuntimeError, match="Another benchmark owns the shared load lock"),
    ):
        run(settings)
    assert not settings.output.exists()
