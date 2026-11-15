import asyncio
import sys
from collections.abc import Awaitable

import erlang
from erlang.supervision import CapabilityUnavailable as NativeCapabilityUnavailable
from erlang.supervision import ChildState, NativeSupervisor, SupervisorError

from pybeam.mailbox import UnboundedMailbox
from pybeam.supervision.config import (
    NativeChildSpec,
    Restart,
    RestartBudget,
    Shutdown,
    Strategy,
    SupervisorConfig,
)
from pybeam.supervision.ref import SupervisorRef


async def expect_native_unavailable(operation: Awaitable[object]) -> None:
    try:
        await operation
    except NativeCapabilityUnavailable:
        pass
    else:
        raise AssertionError("bounded native operation did not fail closed")


async def exercise(parent_process: object) -> bool:
    assert sys.modules["erlang.supervision"] is erlang.supervision
    parent = SupervisorRef.attach(parent_process, mailbox=UnboundedMailbox(), max_children=None)
    bounded_native = NativeSupervisor(
        parent_process, capacity=128, max_bytes=4096, max_children=None
    )
    await expect_native_unavailable(bounded_native.children())
    finite_children = NativeSupervisor(
        parent_process, capacity=None, max_bytes=None, max_children=128
    )
    await expect_native_unavailable(finite_children.children())
    native = NativeSupervisor(parent_process, capacity=None, max_bytes=None, max_children=None)
    await expect_native_unavailable(
        native.start_child(
            b"blocked",
            "py_supervision_tests",
            "start_link",
            ("normal",),
            "permanent",
            50,
            capacity=128,
            max_bytes=4096,
        )
    )
    await expect_native_unavailable(
        native.start_supervisor(
            b"blocked-subtree", "one_for_one", 3, 5, capacity=128, max_bytes=4096, max_children=None
        )
    )
    await expect_native_unavailable(
        native.start_supervisor(
            b"blocked-subtree", "one_for_one", 3, 5, capacity=None, max_bytes=None, max_children=128
        )
    )
    assert await native.children() == ()
    tree = await parent.start_supervisor(
        b"python-api",
        SupervisorConfig(
            mailbox=UnboundedMailbox(),
            max_children=None,
            strategy=Strategy.ONE_FOR_ONE,
            budget=RestartBudget(1, 30),
        ),
    )
    tree_process = (await parent.children())[0].process
    spec = NativeChildSpec(
        b"worker", "py_supervision_tests", mailbox=UnboundedMailbox(), args=("normal",)
    )
    ignored = await tree.start_child(
        NativeChildSpec(
            b"ignored", "py_supervision_tests", mailbox=UnboundedMailbox(), function="ignore"
        )
    )
    assert ignored.state is ChildState.STOPPED
    assert ignored.process is None
    await tree.delete_child(b"ignored")
    # The native test worker ignores mode except when testing stubborn shutdown.
    first = (await tree.start_child(spec)).process
    rows = await tree.children()
    assert len(rows) == 1
    assert rows[0].process == first
    assert rows[0].state is ChildState.RUNNING
    try:
        await tree.start_child(spec)
    except SupervisorError:
        pass
    else:
        raise AssertionError("duplicate start succeeded")
    await tree.stop_child(b"worker")
    rows = await tree.children()
    assert rows[0].state is ChildState.STOPPED
    second = (await tree.restart_child(b"worker")).process
    assert second != first
    await erlang.async_call("_supervision_test_finish", second, b"failure")
    async with asyncio.timeout(2):
        while True:
            rows = await tree.children()
            if rows[0].process != second and rows[0].state is ChildState.RUNNING:
                break
            await asyncio.sleep(0.002)
    third = rows[0].process
    assert third != first
    await tree.stop_child(b"worker")
    await tree.delete_child(b"worker")
    assert await tree.children() == ()
    temporary = await tree.start_child(
        NativeChildSpec(
            b"temporary",
            "py_supervision_tests",
            mailbox=UnboundedMailbox(),
            args=("normal",),
            restart=Restart.TEMPORARY,
            shutdown=Shutdown(30),
        )
    )
    await erlang.async_call("_supervision_test_finish", temporary.process, b"normal")
    async with asyncio.timeout(2):
        for _ in range(1000):
            if not await tree.children():
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("temporary child did not leave the tree")
    storm = await tree.start_child(
        NativeChildSpec(
            b"storm", "py_supervision_tests", mailbox=UnboundedMailbox(), args=("normal",)
        )
    )
    await erlang.async_call("_supervision_test_finish", storm.process, b"failure")
    async with asyncio.timeout(2):
        while True:
            parent_rows = await parent.children()
            if (
                parent_rows[0].process != tree_process
                and parent_rows[0].state is ChildState.RUNNING
            ):
                break
            await asyncio.sleep(0.002)
    try:
        await tree.children()
    except SupervisorError:
        pass
    else:
        raise AssertionError("stale supervisor PID was accepted")
    replacement = SupervisorRef.attach(
        parent_rows[0].process, mailbox=UnboundedMailbox(), max_children=None
    )
    assert await replacement.children() == ()
    await parent.stop_child(b"python-api")
    await parent.delete_child(b"python-api")
    assert await parent.children() == ()
    return True
