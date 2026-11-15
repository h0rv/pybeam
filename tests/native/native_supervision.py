import asyncio

from pybeam.mailbox import BoundedMailbox, UnboundedMailbox
from pybeam.supervision.config import (
    NativeChildSpec,
    Restart,
    RestartBudget,
    Shutdown,
    Strategy,
    SupervisorConfig,
)
from pybeam.supervision.errors import CapabilityUnavailable
from pybeam.supervision.ref import SupervisorRef


async def main(parent_process: object, event_channel: object) -> bool:
    from _erlang_impl._supervision import ChildState

    from tests.native.supervision_support import DemoCounter, DemoEvents, Started, Stopped

    print(
        "Internal fixture: explicit unbounded mailboxes; native OTP demo counter.",
        flush=True,
    )
    bounded = SupervisorRef.attach(
        parent_process, mailbox=BoundedMailbox(capacity=128, max_bytes=65536), max_children=None
    )
    try:
        await bounded.children()
    except CapabilityUnavailable:
        print("bounded control unavailable: native admission gate is not connected", flush=True)
    else:
        raise RuntimeError("bounded control unexpectedly succeeded without a native gate")

    parent = SupervisorRef.attach(parent_process, mailbox=UnboundedMailbox(), max_children=None)
    events = DemoEvents.attach(event_channel)
    tree = await parent.start_supervisor(
        b"example",
        SupervisorConfig(
            mailbox=UnboundedMailbox(),
            max_children=None,
            strategy=Strategy.ONE_FOR_ONE,
            budget=RestartBudget(max_restarts=3, window_seconds=5),
        ),
    )
    try:
        async with asyncio.timeout(5):
            first = await tree.start_child(
                NativeChildSpec(
                    b"counter",
                    "py_supervision_demo",
                    mailbox=UnboundedMailbox(),
                    args=(event_channel,),
                    restart=Restart.PERMANENT,
                    shutdown=Shutdown(200),
                )
            )
            started = await events.next_event()
            children = await tree.children()
            if (
                not isinstance(started, Started)
                or started.process != first.process
                or len(children) != 1
                or children[0].state is not ChildState.RUNNING
                or children[0].process != first.process
            ):
                raise RuntimeError("native start and inspection did not agree")
            counter = DemoCounter(first.process, capacity=None, max_bytes=None)
            value = await counter.value()
            if value != 0 or await counter.increment() != 1:
                raise RuntimeError("native demo counter did not initialize and increment")
            print("started pid={} state={}".format(first.process, value), flush=True)
            print("incremented state={}".format(await counter.value()), flush=True)

            await counter.crash()
            stopped = await events.next_event()
            restarted = await events.next_event()
            if (
                not isinstance(stopped, Stopped)
                or stopped.process != first.process
                or stopped.reason != "deliberate_crash"
                or not isinstance(restarted, Started)
                or restarted.process == first.process
            ):
                raise RuntimeError("OTP restart was not confirmed by lifecycle events")
            replacement = DemoCounter(restarted.process, capacity=None, max_bytes=None)
            children = await tree.children()
            value = await replacement.value()
            if value != 0 or len(children) != 1 or children[0].process != restarted.process:
                raise RuntimeError("replacement PID and reset state were not confirmed")
            print(
                "restart old_pid={} new_pid={} state={}".format(
                    first.process, restarted.process, value
                ),
                flush=True,
            )

            await tree.stop_child(b"counter")
            stopped = await events.next_event()
            if (
                not isinstance(stopped, Stopped)
                or stopped.process != restarted.process
                or stopped.reason != "shutdown"
            ):
                raise RuntimeError("graceful worker shutdown was not confirmed")
            children = await tree.children()
            if (
                len(children) != 1
                or children[0].state is not ChildState.STOPPED
                or children[0].process is not None
            ):
                raise RuntimeError("stopped child specification was not retained")
            await tree.delete_child(b"counter")
            if await tree.children():
                raise RuntimeError("child specification was not deleted")
            print("worker shutdown reason={}".format(stopped.reason), flush=True)
    finally:
        async with asyncio.timeout(3):
            await parent.stop_child(b"example")
            await parent.delete_child(b"example")
    if await parent.children():
        raise RuntimeError("application supervisor retained example children")
    print("shutdown children=0", flush=True)
    return True
