import asyncio
import sys
import threading
import traceback

from pybeam.runtime.loader import entry_globals, entry_loop

_namespace: dict[str, object] = {}


def snapshot(_request: str) -> str:
    """Format every other Python thread's stack and every task on the entry loop."""
    current = threading.get_ident()
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    lines: list[str] = []
    for ident, frame in sys._current_frames().items():
        if ident != current:
            lines.append("Thread " + names.get(ident, str(ident)) + "\n")
            lines.extend(traceback.format_stack(frame))
            lines.append("\n")
    loop = entry_loop()
    if loop is not None:
        for task in sorted(asyncio.all_tasks(loop), key=asyncio.Task.get_name):
            lines.append(asyncio.format_call_graph(task) + "\n")
    return "".join(lines)


def evaluate(request: str) -> str:
    """Run debugger input against a copy of the entry file's globals.

    Expressions return their repr. Statements run for their effect; their
    output goes to the application's own stdout.
    """
    if not _namespace:
        _namespace.update(entry_globals())
        _namespace["__name__"] = "__pybeam_debug__"
    try:
        try:
            code = compile(request, "<pybeam debug>", "eval")
        except SyntaxError:
            exec(compile(request, "<pybeam debug>", "exec"), _namespace)  # noqa: S102
            return ""
        return repr(eval(code, _namespace))  # noqa: S307
    except Exception:
        return traceback.format_exc()
