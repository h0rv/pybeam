import asyncio
import selectors
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import override

import pytest

from tests.native.genstage_shutdown import Operations, consume, preserve_cleanup
from tests.readiness import MAX_OUTPUT_BYTES, ProcessOutput, ReadinessFailure, wait_ready


@dataclass
class VirtualSource:
    events: list[tuple[float, bytes]] = field(default_factory=list)
    exit_at: float | None = None
    now: float = 0
    waits: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    def poll(self) -> int | None:
        return 7 if self.exit_at is not None and self.now >= self.exit_at else None

    def read(self, timeout: float) -> bytes | None:
        self.waits.append(timeout)
        limit = self.now + timeout
        if self.exit_at is not None:
            limit = min(limit, self.exit_at)
        if self.events and self.events[0][0] <= limit:
            self.now, payload = self.events.pop(0)
            return payload
        self.now = limit
        return None


def test_readiness__accepts_fragmented_ready_after_a_silent_second() -> None:
    source = VirtualSource([(1.25, b"PHASE verified\nSHUT"), (1.5, b"DOWN_REA"), (1.75, b"DY\n")])
    output = bytearray()
    wait_ready(source, output, clock=source.clock)
    assert source.now == 1.75
    assert output == b"PHASE verified\nSHUTDOWN_READY\n"


def test_readiness__silence_fails_at_the_existing_ten_second_deadline() -> None:
    source = VirtualSource()
    with pytest.raises(ReadinessFailure, match="after 10 seconds"):
        wait_ready(source, bytearray(), clock=source.clock)
    assert source.now == 10
    assert source.waits == [1.0] * 10


def test_readiness__bounds_the_last_wait_by_remaining_time() -> None:
    source = VirtualSource([(9.75, b"PHASE activation_pending\n"), (10.1, b"SHUTDOWN_READY\n")])
    with pytest.raises(ReadinessFailure, match="activation_pending"):
        wait_ready(source, bytearray(), clock=source.clock)
    assert source.now == 10
    assert source.waits[-1] == 0.25


@pytest.mark.parametrize(
    "exit_at,events,reason",
    [(None, [(0.2, b"err\n"), (0.3, b"")], "EOF"), (0.3, [(0.2, b"err\n")], "Child exited")],
)
def test_readiness__fails_immediately_on_eof_or_exit_with_output(
    exit_at: float | None, events: list[tuple[float, bytes]], reason: str
) -> None:
    source = VirtualSource(events, exit_at=exit_at)
    with pytest.raises(ReadinessFailure, match=reason) as captured:
        wait_ready(source, bytearray(), clock=source.clock)
    assert source.now == 0.3
    assert "err" in str(captured.value)


def test_readiness__bounds_captured_output() -> None:
    source = VirtualSource([(0.1, b"x" * (MAX_OUTPUT_BYTES + 1))])
    output = bytearray()
    with pytest.raises(ReadinessFailure, match="exceeded 65536 bytes"):
        wait_ready(source, output, clock=source.clock)
    assert len(output) == MAX_OUTPUT_BYTES


def test_readiness__controlled_child_survives_one_silent_poll() -> None:
    with subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; sys.stdin.buffer.read(1); print('PHASE verified'); sys.stdout.write('SHUTDOWN_'); sys.stdout.flush(); print('READY'); sys.stdin.buffer.read(1)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as child:

        class ControlledOutput(ProcessOutput):
            released = False

            @override
            def read(self, timeout: float) -> bytes | None:
                chunk = super().read(timeout)
                if chunk is None and not self.released:
                    assert child.stdin is not None
                    child.stdin.write(b"g")
                    child.stdin.flush()
                    self.released = True
                return chunk

        try:
            with selectors.DefaultSelector() as selector:
                source = ControlledOutput(child, selector)
                output = bytearray()
                wait_ready(source, output)
                assert source.released
                assert b"SHUTDOWN_READY\n" in output
            child.communicate(input=b"s", timeout=5)
            assert child.returncode == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


@pytest.mark.asyncio
async def test_phase_cancel__before_activation_does_not_enter_graph() -> None:
    activating = asyncio.Event()
    messages: list[str] = []
    requests: list[str] = []

    async def activate() -> None:
        requests.append("activation")
        activating.set()
        await asyncio.Event().wait()

    @asynccontextmanager
    async def graph() -> AsyncGenerator[AsyncIterator[bytes]]:
        requests.append("invalid_graph_rpc")
        yield empty_events()

    async def contexts() -> int:
        requests.append("core_counters")
        return 1

    task = asyncio.create_task(consume(Operations(activate, graph, contexts, messages.append), 1))
    await activating.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert requests == ["activation", "core_counters"]
    assert "PHASE stop phase=activation_pending" in messages
    assert messages[-1] == "SHUTDOWN_CONTEXTS 1"


async def empty_events() -> AsyncIterator[bytes]:
    values: tuple[bytes, ...] = ()
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_phase_cancel__owned_graph_closes_before_core_context_probe() -> None:
    owned = asyncio.Event()
    requests: list[str] = []
    messages: list[str] = []

    async def activate() -> None:
        requests.append("registration_ack")

    async def events() -> AsyncIterator[bytes]:
        owned.set()
        await asyncio.Event().wait()
        yield b""

    @asynccontextmanager
    async def graph() -> AsyncGenerator[AsyncIterator[bytes]]:
        requests.append("graph_created")
        try:
            yield events()
        finally:
            requests.append("graph_close_ack")

    async def contexts() -> int:
        requests.append("core_counters")
        return 1

    task = asyncio.create_task(consume(Operations(activate, graph, contexts, messages.append), 1))
    await owned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert requests == ["registration_ack", "graph_created", "graph_close_ack", "core_counters"]
    assert "PHASE graph_created" in messages
    assert "PHASE stop phase=graph_owned" in messages
    assert messages[-1] == "SHUTDOWN_CONTEXTS 1"


@pytest.mark.asyncio
async def test_phase_cancel__pending_acquisition_waits_for_its_cleanup_ack() -> None:
    acquiring = asyncio.Event()
    finish_acquisition = asyncio.Event()
    requests: list[str] = []
    messages: list[str] = []

    async def activate() -> None:
        return None

    async def acquire() -> None:
        acquiring.set()
        await finish_acquisition.wait()
        requests.append("graph_created_after_cancel")

    @asynccontextmanager
    async def graph() -> AsyncGenerator[AsyncIterator[bytes]]:
        opening = asyncio.create_task(acquire())
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            await opening
            requests.append("graph_close_ack")
            raise
        yield empty_events()

    async def contexts() -> int:
        requests.append("core_counters")
        return 1

    task = asyncio.create_task(consume(Operations(activate, graph, contexts, messages.append), 1))
    try:
        await acquiring.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert requests == []
    finally:
        finish_acquisition.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert requests == ["graph_created_after_cancel", "graph_close_ack", "core_counters"]
    assert "PHASE stop phase=graph_request_pending" in messages


@pytest.mark.asyncio
async def test_phase_cancel__preserves_both_original_cancel_and_cleanup_failure() -> None:
    async def work() -> None:
        raise asyncio.CancelledError("original cancellation")

    async def cleanup() -> None:
        raise RuntimeError("drain failed")

    with pytest.raises(BaseExceptionGroup) as captured:
        await preserve_cleanup(work(), cleanup)
    assert isinstance(captured.value.exceptions[0], asyncio.CancelledError)
    assert isinstance(captured.value.exceptions[1], RuntimeError)
