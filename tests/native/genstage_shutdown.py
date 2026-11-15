import argparse
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pybeam
from pybeam.actors import context_counts
from pybeam.integrations.genstage.pipeline import stream
from pybeam.runtime.optional import read_manifest, start_optional


class Phase(StrEnum):
    ACTIVATING = "activation_pending"
    REGISTERED = "application_registered"
    OPENING = "graph_request_pending"
    OWNED = "graph_owned"


def emit(message: str) -> None:
    print(message, flush=True)


async def activate() -> None:
    root = Path(pybeam.__file__).parent / "runtime/_assets/optional/genstage"
    emit("PHASE manifest_verification_begin")
    await asyncio.to_thread(read_manifest, root)
    emit("PHASE manifest_verified")
    emit("PHASE application_activation_begin")
    await start_optional("genstage")
    emit("PHASE application_registration_acknowledged")


async def live_contexts() -> int:
    return (await context_counts()).live


def graph() -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
    from genstage_callbacks import process

    return stream(16, process, action="slow")


@dataclass(frozen=True, slots=True)
class Operations:
    activate: Callable[[], Awaitable[None]]
    graph: Callable[[], AbstractAsyncContextManager[AsyncIterator[bytes]]]
    contexts: Callable[[], Awaitable[int]]
    emit: Callable[[str], None]


async def preserve_cleanup(work: Awaitable[None], cleanup: Callable[[], Awaitable[None]]) -> None:
    try:
        await work
    except BaseException as original:
        try:
            await cleanup()
        except BaseException as failure:
            raise BaseExceptionGroup(
                "Fixture operation and cleanup failed", [original, failure]
            ) from None
        raise
    else:
        await cleanup()


async def consume(operations: Operations, baseline: int) -> None:
    phase = Phase.ACTIVATING

    async def work() -> None:
        nonlocal phase
        await operations.activate()
        phase = Phase.REGISTERED
        operations.emit("PHASE application_registered")
        phase = Phase.OPENING
        operations.emit("PHASE graph_request_begin")
        async with operations.graph() as events:
            phase = Phase.OWNED
            operations.emit("PHASE graph_created")
            operations.emit("SHUTDOWN_READY")
            async for _ in events:
                pass

    async def cleanup() -> None:
        operations.emit("PHASE stop phase=" + phase.value)
        # Core counters are valid even when optional application registration was interrupted.
        live = await operations.contexts()
        if live != baseline:
            raise AssertionError("Native contexts did not drain: " + str(live))
        operations.emit("PHASE context_drained " + str(live))
        operations.emit("SHUTDOWN_CONTEXTS " + str(live))

    await preserve_cleanup(work(), cleanup)


@pybeam.actor
class Counter:
    async def acknowledge(self, value: int) -> int:
        return value


async def main(*, before_activation: bool = False) -> None:
    baseline = await live_contexts()
    emit("PHASE owner_context_baseline " + str(baseline))

    async def activation() -> None:
        if before_activation:
            emit("PHASE activation_control_wait")
            emit("BEFORE_ACTIVATION_READY")
            await asyncio.Event().wait()
        await activate()

    operations = Operations(activation, graph, live_contexts, emit)

    async def work() -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(consume(operations, baseline))
            await asyncio.Event().wait()

    async def cleanup() -> None:
        async with pybeam.spawn(Counter) as counter:
            acknowledged = await counter.acknowledge(42)
            if acknowledged != 42:
                raise AssertionError("Counter did not acknowledge after graph cleanup")
            emit("COUNTER_ACK " + str(acknowledged))
        live = await live_contexts()
        if live != baseline:
            raise AssertionError("Counter context did not drain")
        emit("PHASE owner_context_drained " + str(live))

    await preserve_cleanup(work(), cleanup)


class Arguments(argparse.Namespace):
    before_activation: bool


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--before-activation", action="store_true")
    arguments = parser.parse_args(namespace=Arguments())

    async def entry() -> None:
        await main(before_activation=arguments.before_activation)

    pybeam.run(entry)
