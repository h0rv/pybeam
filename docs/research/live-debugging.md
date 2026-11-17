# Debug a running app from another terminal

A running PyBeam app can already show every Python stack and asyncio task to a second terminal, through BEAM distribution and without root. Stock `pdb -p` cannot attach to the embedded interpreter. A small `pybeam debug` command can hide the BEAM steps from Python users.

## Evidence boundary

Tested on Linux with Python 3.14.10 (conventional build), OTP 29.1.2, PyBeam `90c9aaa`, and erlang-python `c007eb5` (the pinned runtime). The app was a single `@pybeam.actor` called from three `asyncio.TaskGroup` workers; after 20 calls the actor method spins in `while True: pass`. Only worker mode was tested. Own-GIL and isolated contexts were not tested. `ptrace_scope` was `1`.

## What works today

| Goal | Tool | Result |
| --- | --- | --- |
| Python stacks for every thread | `_remote_debugging.RemoteUnwinder(pid, all_threads=True)` with `sudo` | Works. The spinning thread shows `Orders.place` at `main.py:14` under `Container.dispatch` |
| asyncio task tree | `sudo python -m asyncio ps <pid>` and `pstree <pid>` | Works, but only for the managed entry loop: `Task-1` awaiting `TaskGroup.__aexit__`. Worker tasks are not listed |
| Stacks, tasks, and app state without root | BEAM distribution, then Python run in a new worker context on the target node | Works. Shows every thread's stack, the spinning actor, and all asyncio tasks of the entry loop, including `worker-0` to `worker-2` |
| Interactive `pdb` | `sudo python -m pdb -p <pid>` | Fails: `RuntimeError: Can't find the main thread in the remote process` |
| Run Python in the entry context | `py_context:exec/2` on the context in `py_context_sup` | Refused: `{error, loop_running}` |
| Stop the spinning actor | `py_context:interrupt/1` on its context | Returns `ok`, but the loop keeps running |

### BEAM distribution path

The managed launcher rejects `distribution` in `[tool.pybeam]`, but the VM reads `ERL_FLAGS`, so a private run can be named:

```sh
ERL_FLAGS="-sname app -setcookie probe" python main.py
```

From a second terminal, a hidden Erlang node can spawn a process on the target that owns a fresh worker context. Worker contexts share the main interpreter, so Python run there sees the same modules as the app:

```erlang
N = list_to_atom("app@" ++ net_adm:localhost()),
pong = net_adm:ping(N),
Self = self(),
spawn(N, fun() ->
    {ok, D} = py_context:new(#{mode => worker}),
    ok = py_context:exec(D, Code),
    Self ! py_context:eval(D, <<"RESULT">>),
    receive stop -> ok end
end).
```

`Code` used only the standard library and the loader's session record:

```python
import asyncio
import sys
import threading
import traceback

from pybeam.runtime import loader

lines = []
for ident, frame in sys._current_frames().items():
    if ident != threading.get_ident():
        lines.append("thread " + str(ident))
        lines.extend(traceback.format_stack(frame)[-3:])
loop = loader._state.session.task.get_loop()
for task in asyncio.all_tasks(loop):
    lines.append("task " + task.get_name() + " " + repr(task.get_coro().cr_frame))
RESULT = "".join(lines)
```

The output named the spinning frame (`main.py`, `place`) and the waiting workers (`main.py:21` and `:22`). The owning process must outlive the RPC because `py_context:new/1` links the context to its caller.

### Why `pdb -p` fails

`pdb -p` uses `sys.remote_exec`, which needs CPython's main thread state. PyBeam runs Python on erlang-python context threads, not on the process's main thread, so the debugger cannot find a target. `RemoteUnwinder` and `asyncio ps` only read memory, so they work, but they need `ptrace` rights and see only the entry loop.

### Why interrupt missed the actor

`py_context:interrupt/1` raises `KeyboardInterrupt` in the thread that runs the context's current call. The actor's context was waiting on an async result while the method ran on a different thread, so the exception went to the wait, not to the spin. This is an inference from the observed stacks; the erlang-python source was not traced.

## Recommended wrapper

Keep BEAM distribution as the transport and erlang-python contexts as the execution. Add no network protocol or debug server.

1. `pybeam run --debug` starts the VM with loopback-only distribution (`-sname`, `-kernel inet_dist_use_interface '{127,0,0,1}'`), a random cookie written to a `0600` file, and the node name next to it. This replaces the `ERL_FLAGS` workaround and keeps `launch()` rejecting general distribution.
2. `pybeam debug stacks` starts a hidden client node from the bundled runtime, creates a debug-owned worker context on the target, and prints every thread's stack and every task on known loops, then closes the context.
3. `pybeam debug shell` is a `code.InteractiveConsole` on the client that sends each input to that context and prints the `repr` of the result. Python developers see a Python prompt; the BEAM node stays hidden.
4. Keep the Erlang remote shell (`erl -remsh`) for operators who need [OTP inspection](observability.md).

## Gaps before this is a feature

- erlang-python needs a supported way to run code on a context whose loop is running (for example `py_context:submit/4` to a debug entry point), and to list each context's event loop. Without it, `stacks` sees only threads and the entry loop.
- Interrupting a spinning actor needs the interrupt to reach the thread that runs the actor method.
- Own-GIL and isolated contexts have their own interpreters. A debug context in the main interpreter cannot see their state; each needs evaluation inside that interpreter.
- A real interactive `pdb` (breakpoints, stepping) needs a runtime hook that runs `pdb` on the target context thread with its input and output sent over distribution. CPython's `pdb -p` will not do this for an embedded interpreter.
- The debug shell runs arbitrary code inside the app. Anyone with the cookie controls the node, and cookies do not encrypt traffic. Keep it loopback-only and opt-in.
- Running debug code holds the GIL in worker mode and can slow or block the app. A slow `repr` delays every worker context.
- Untested: free-threaded builds, macOS, Hornbeam apps, and cleanup when the client disconnects mid-command.
