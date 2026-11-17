import asyncio
from pathlib import Path

import pytest

from pybeam import queues
from pybeam.queues import DurableQueue, LeaseExpired, PayloadTooLarge, QueueFull, QueueStoreError
from pybeam.runtime.config import BootstrapError, load_config, parse_config


@pytest.fixture(autouse=True)
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


def test_config(tmp_path: Path) -> None:
    assert parse_config({}, tmp_path).data_dir == tmp_path / ".pybeam/data"
    assert parse_config({"data_dir": "store"}, tmp_path).data_dir == tmp_path / "store"
    assert (
        parse_config({"data_dir": tmp_path / "absolute"}, tmp_path).data_dir
        == tmp_path / "absolute"
    )
    for value in (None, 1, ""):
        with pytest.raises(BootstrapError):
            parse_config({"data_dir": value}, tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.pybeam]\ndata_dir = "store"\n')
    assert load_config(tmp_path / "nested").data_dir == tmp_path / "store"


def test_default_config_without_project(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nested").data_dir == tmp_path / "nested/.pybeam/data"


@pytest.mark.parametrize("max_items", [0, -1, True])
def test_capacity(max_items: int) -> None:
    with pytest.raises(ValueError):
        DurableQueue("q", max_items=max_items)


def test_name() -> None:
    with pytest.raises(ValueError):
        DurableQueue("")


@pytest.mark.parametrize("lease", [0, -1, float("inf"), float("nan"), True, 4_294_968])
async def test_lease(lease: float) -> None:
    with pytest.raises(ValueError):
        async with DurableQueue("q").get(lease=lease):
            pytest.fail("Invalid lease was accepted")


async def test_payload() -> None:
    with pytest.raises(PayloadTooLarge):
        await DurableQueue("q").put(b"x" * 1_048_577)


async def test_put(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    stored = True

    async def call(name: str, *args: object) -> object:
        calls.append((name, args))
        if name == "_queue_open":
            return "ok"
        return ("ok", "stored" if stored else "duplicate")

    monkeypatch.setattr(queues, "_call", call)
    queue = DurableQueue("q", max_items=2)
    assert await queue.put(b"", key="key")
    stored = False
    assert not await queue.put(b"", key="key")
    assert calls == [
        ("_queue_open", (str(tmp_path / ".pybeam/data"), b"q", 2)),
        ("_queue_put", (b"q", b"key", b"")),
        ("_queue_put", (b"q", b"key", b"")),
    ]


@pytest.mark.parametrize("fail", [False, True])
async def test_context(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    calls: list[str] = []

    async def call(name: str, *args: object) -> object:
        calls.append(name)
        if name == "_queue_claim":
            return ("ok", 1, b"token", 2, b"payload")
        return "ok"

    monkeypatch.setattr(queues, "_call", call)
    try:
        async with DurableQueue("q").get() as item:
            assert item.payload == b"payload" and item.attempt == 2
            assert hash(item) == hash(queues.QueueItem(b"payload", 2))
            if fail:
                raise LookupError("retry")
    except LookupError:
        assert fail
    assert calls[-1] == ("_queue_release" if fail else "_queue_ack")


async def test_late_claim_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    released = asyncio.Event()

    async def call(name: str, *args: object) -> object:
        if name == "_queue_claim":
            started.set()
            await finish.wait()
            return ("ok", 1, b"token", 1, b"payload")
        if name == "_queue_release":
            released.set()
        return "ok"

    async def receive() -> None:
        async with DurableQueue("q").get():
            pytest.fail("Cancelled delivery reached the body")

    monkeypatch.setattr(queues, "_call", call)
    pending = asyncio.create_task(receive())
    await started.wait()
    pending.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert released.is_set()


class Native:
    def __init__(self, result: object) -> None:
        self.result = result

    async def async_call(self, name: str, *args: object) -> object:
        return self.result


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        ("full", QueueFull),
        ("stale_lease", LeaseExpired),
        ("payload_too_large", PayloadTooLarge),
        (("sync_failed", "disk"), QueueStoreError),
    ],
)
async def test_errors(
    monkeypatch: pytest.MonkeyPatch, reason: object, error: type[Exception]
) -> None:
    monkeypatch.setattr(queues, "erlang", Native(("error", reason)))
    with pytest.raises(error):
        await queues._call("_queue_ack", 1, b"token")


async def test_no_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queues, "erlang", None)
    with pytest.raises(QueueStoreError, match="managed runtime"):
        await DurableQueue("q").put(b"")


@pytest.mark.parametrize("response", [None, ("ok",), ("ok", 1, b"t", 0, b"v")])
def test_invalid_claim(response: object) -> None:
    with pytest.raises(QueueStoreError):
        queues._claim(response)


async def test_empty_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    replies: list[object] = ["empty", ("ok", 1, b"t", 1, b"")]

    async def call(name: str, *args: object) -> object:
        return replies.pop(0) if name == "_queue_claim" else "ok"

    monkeypatch.setattr(queues, "_call", call)
    async with DurableQueue("q").get() as item:
        assert item.attempt == 1


@pytest.mark.parametrize("response", ["wrong", ("ok", "unknown")])
async def test_invalid_put(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    async def call(name: str, *args: object) -> object:
        return "ok" if name == "_queue_open" else response

    monkeypatch.setattr(queues, "_call", call)
    with pytest.raises(QueueStoreError):
        await DurableQueue("q").put(b"")
