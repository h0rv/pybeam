import asyncio
import json
import os
import runpy
import sys
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pybeam.runtime.config import BootstrapError

type Entry = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class _Session:
    ready: Path
    completed: Path
    entry: Entry | None = None
    invoked: bool = False
    task: asyncio.Task[object] | None = None
    load_status: int = 0


@dataclass(slots=True)
class _Loader:
    session: _Session | None = None


_state = _Loader()


def register(entry: Entry) -> bool:
    if _state.session is None:
        return False
    if _state.session.entry is not None:
        raise BootstrapError("A Python entry file must call pybeam.run exactly once")
    _state.session.entry = entry
    return True


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        raise BootstrapError("Invalid managed bootstrap arguments")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise BootstrapError("Invalid managed bootstrap argument")
        result.append(item)
    return result


def _status(error: BaseException) -> int:
    if isinstance(error, SystemExit):
        match error.code:
            case None:
                return 0
            case int() as code:
                return code if 0 <= code <= 255 else 1
            case message:
                print(message, file=sys.stderr)
                return 1
    traceback.print_exception(error)
    return 1


def load() -> None:
    if _state.session is not None:
        raise BootstrapError("The managed entry file has already been loaded")
    payload: object = json.loads(os.environ.pop("PYBEAM_BOOTSTRAP"))
    if not isinstance(payload, dict):
        raise BootstrapError("Invalid managed bootstrap payload")
    argv = _strings(payload.get("argv"))
    ready: object = payload.get("ready")
    completed: object = payload.get("completed")
    if not argv or not isinstance(ready, str) or not isinstance(completed, str):
        raise BootstrapError("Missing managed entry file or lifecycle paths")
    session = _Session(ready=Path(ready), completed=Path(completed))
    _state.session = session
    sys.argv[:] = argv
    sys.path.insert(0, str(Path(argv[0]).parent))
    try:
        # File initialization deliberately runs again inside the embedded interpreter.
        runpy.run_path(argv[0], run_name="__main__")
        if session.entry is None:
            raise BootstrapError("The entry file did not register an async main with pybeam.run")
    except BaseException as error:
        session.load_status = _status(error)
        session.invoked = True


async def invoke() -> int:
    session = _state.session
    if session is None:
        raise BootstrapError("No managed entry file was loaded")
    try:
        session.ready.touch()
        if session.invoked:
            return session.load_status
        session.invoked = True
        entry = session.entry
        if entry is None:
            raise BootstrapError("No async main was registered")
        session.task = asyncio.current_task()
        await entry()
    except asyncio.CancelledError:
        session.load_status = 130
    except BaseException as error:
        session.load_status = _status(error)
    finally:
        pending = session.completed.with_suffix(".tmp")
        pending.write_text(str(session.load_status))
        pending.replace(session.completed)
        sys.stdout.flush()
        sys.stderr.flush()
    return session.load_status


async def shutdown(timeout_ms: int) -> None:
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    session = _state.session
    if session is not None and session.task is not None and not session.task.done():
        entry = session.task
        if not entry.cancelling():
            entry.cancel()
        # The entrypoint owns framework lifespans, including tasks created by
        # libraries without an asyncio awaited-by relationship.
        done, _ = await asyncio.wait({entry}, timeout=max(0.0, deadline - loop.time()))
        if not done:
            raise BootstrapError("Managed entrypoint did not finish shutdown before the deadline")
        if not entry.cancelled() and (error := entry.exception()) is not None:
            raise error
    cancelled: set[asyncio.Task[object]] = set()
    while True:
        if loop.time() >= deadline:
            raise BootstrapError("Python tasks did not quiesce before the shutdown deadline")
        tasks = {task for task in asyncio.all_tasks() if task is not current}
        session = _state.session
        if session is not None and session.task is not None and not session.task.done():
            tasks.add(session.task)
        if not tasks:
            # Let already queued cancellation callbacks create their successor tasks.
            await asyncio.sleep(0)
            if not any(task is not current for task in asyncio.all_tasks()):
                if loop.time() >= deadline:
                    raise BootstrapError(
                        "Python tasks did not quiesce before the shutdown deadline"
                    )
                return
            continue
        for task in tasks - cancelled:
            graph = asyncio.capture_call_graph(task, limit=0)
            if graph is None:
                raise BootstrapError("Cannot inspect pending cleanup task")
            # TaskGroups and ordinary awaits propagate their owner's cancellation.
            # Cancelling their children as well would interrupt cooperative finalizers.
            if any(parent.future in tasks for parent in graph.awaited_by):
                continue
            if not task.cancelling():
                task.cancel()
            cancelled.add(task)
        done, _ = await asyncio.wait(
            tasks,
            timeout=max(0.0, min(0.01, deadline - loop.time())),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    raise error
