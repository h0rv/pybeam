import asyncio
from functools import partial
from pathlib import Path

import pytest

from pybeam import queues
from pybeam.queues import DurableQueue, DurableTasks, PayloadTooLarge, QueueFull, QueueStoreError
from tests.test_queues import Native


async def body(payload: bytes) -> bytes:
    return payload


def sync_body(payload: bytes) -> asyncio.Future[bytes]:
    future = asyncio.Future[bytes]()
    future.set_result(payload)
    return future


class CallableBody:
    async def __call__(self, payload: bytes) -> bytes:
        return payload


@pytest.fixture(autouse=True)
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("replicas", ["node@host", ("",), ("node\x00host",)])
def test_replicas(replicas: str | tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        DurableQueue("q", replicas=replicas)
    with pytest.raises(ValueError):
        DurableTasks("q", replicas=replicas)


async def test_submit_and_replica_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    responses: list[object] = [("ok", "stored"), ("ok", "duplicate")]

    async def call(name: str, *args: object) -> object:
        calls.append((name, args))
        return "ok" if name == "_queue_open" else responses.pop(0)

    monkeypatch.setattr(queues, "_call", call)
    replicas = ["a@host", "b@host", "c@host"]
    tasks = DurableTasks("q", max_items=2, replicas=replicas)
    replicas.clear()
    assert await tasks.submit(body, b"", key="key")
    assert not await tasks.submit(body, b"\x00\xff", key="key")
    assert calls == [
        (
            "_queue_open",
            (str(tmp_path / ".pybeam/data"), b"q", 2, [b"a@host", b"b@host", b"c@host"]),
        ),
        ("_tasks_submit", (b"q", b"key", b"tests.test_durable_tasks", b"body", b"")),
        ("_tasks_submit", (b"q", b"key", b"tests.test_durable_tasks", b"body", b"\x00\xff")),
    ]


async def test_unkeyed_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def call(name: str, *args: object) -> object:
        if name == "_tasks_submit":
            assert args[1] is None
            return ("ok", "stored")
        return "ok"

    monkeypatch.setattr(queues, "_call", call)
    assert await DurableTasks("q").submit(body, b"value")


async def test_invalid_function() -> None:
    async def nested(payload: bytes) -> bytes:
        return payload

    with pytest.raises(TypeError, match="plain async"):
        await DurableTasks("q").submit(CallableBody(), b"")
    with pytest.raises(TypeError, match="plain async"):
        await DurableTasks("q").submit(partial(body), b"")
    with pytest.raises(TypeError, match="top-level"):
        await DurableTasks("q").submit(nested, b"")


async def test_sync_function() -> None:
    with pytest.raises(TypeError, match="plain async"):
        await DurableTasks("q").submit(sync_body, b"")


@pytest.mark.parametrize("module", ["", "bad-name", "module..name"])
async def test_invalid_module(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    monkeypatch.setattr(body, "__module__", module)
    with pytest.raises(TypeError, match="top-level"):
        await DurableTasks("q").submit(body, b"")


async def test_payload_limit() -> None:
    with pytest.raises(PayloadTooLarge):
        await DurableTasks("q").submit(body, b"x" * 1_048_577)


@pytest.mark.parametrize("response", [None, ("ok", "unknown")])
async def test_invalid_submit_response(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    async def call(name: str, *args: object) -> object:
        return "ok" if name == "_queue_open" else response

    monkeypatch.setattr(queues, "_call", call)
    with pytest.raises(QueueStoreError, match="Invalid put response"):
        await DurableTasks("q").submit(body, b"")


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        ("full", QueueFull),
        ("payload_too_large", PayloadTooLarge),
        ("disk_failure", QueueStoreError),
    ],
)
async def test_submit_errors(
    monkeypatch: pytest.MonkeyPatch, reason: str, error: type[Exception]
) -> None:
    native = Native("ok")
    monkeypatch.setattr(queues, "erlang", native)
    tasks = DurableTasks("q")
    await tasks._queue._open()
    native.result = ("error", reason)
    with pytest.raises(error):
        await tasks.submit(body, b"")


@pytest.mark.parametrize("concurrency", [0, -1, True, 1025])
async def test_concurrency(concurrency: int) -> None:
    with pytest.raises(ValueError):
        async with DurableTasks("q").serve(concurrency=concurrency):
            pytest.fail("Invalid concurrency was accepted")


@pytest.mark.parametrize("lease", [0, -1, True, float("nan"), float("inf"), 4_294_968])
async def test_lease(lease: float) -> None:
    with pytest.raises(ValueError):
        async with DurableTasks("q").serve(lease=lease):
            pytest.fail("Invalid lease was accepted")


@pytest.mark.parametrize("fail", [False, True])
async def test_serve(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    async def call(name: str, *args: object) -> object:
        calls.append((name, args))
        return "ok"

    monkeypatch.setattr(queues, "_call", call)
    try:
        async with DurableTasks("q").serve(concurrency=2, lease=0.0001) as serving:
            assert serving is None
            if fail:
                raise LookupError("body failed")
    except LookupError:
        assert fail
    assert calls[-2:] == [("_tasks_serve", (b"q", 2, 1)), ("_tasks_stop", (b"q",))]


@pytest.mark.parametrize("operation", ["enter", "body", "exit"])
async def test_serve_cancellation(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    reached = asyncio.Event()
    finish = asyncio.Event()
    stopped = asyncio.Event()

    async def call(name: str, *args: object) -> object:
        if (operation == "enter" and name == "_tasks_serve") or (
            operation == "exit" and name == "_tasks_stop"
        ):
            reached.set()
            await finish.wait()
        if name == "_tasks_stop":
            stopped.set()
        return "ok"

    async def serve() -> None:
        async with DurableTasks("q").serve():
            if operation == "body":
                reached.set()
                await finish.wait()

    monkeypatch.setattr(queues, "_call", call)
    pending = asyncio.create_task(serve())
    await reached.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stopped.is_set()


@pytest.mark.parametrize("operation", ["_queue_open", "_tasks_serve", "_tasks_stop"])
async def test_invalid_control_response(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    async def call(name: str, *args: object) -> object:
        return "invalid" if name == operation else "ok"

    monkeypatch.setattr(queues, "_call", call)
    with pytest.raises(QueueStoreError):
        async with DurableTasks("q").serve():
            pass


async def test_failed_enter_does_not_stop_other_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def call(name: str, *args: object) -> object:
        calls.append(name)
        if name == "_tasks_serve":
            raise QueueStoreError("already_serving")
        return "ok"

    monkeypatch.setattr(queues, "_call", call)
    with pytest.raises(QueueStoreError, match="already_serving"):
        async with DurableTasks("q").serve():
            pytest.fail("Failed enter yielded")
    assert "_tasks_stop" not in calls


async def test_submit_cancellation_is_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def call(name: str, *args: object) -> object:
        if name == "_tasks_submit":
            started.set()
            await asyncio.Event().wait()
        return "ok"

    monkeypatch.setattr(queues, "_call", call)
    pending = asyncio.create_task(DurableTasks("q").submit(body, b"value"))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
