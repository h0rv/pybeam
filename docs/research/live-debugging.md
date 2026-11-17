# Debug a running app from another terminal

A running PyBeam app can already show every Python stack and asyncio task to a second terminal, through BEAM distribution and without root. With an erlang-python change, stock `pdb -p` and the Python 3.15 sampling profiler attach too. A spiked `pybeam debug` command hides the BEAM steps from Python users.

## Evidence boundary

Tested on Linux with Python 3.14.10 (conventional build), OTP 29.1.2, PyBeam `90c9aaa`, and erlang-python `c007eb5` (the runtime pinned for this experiment). The app was a single `@pybeam.actor` called from three `asyncio.TaskGroup` workers; after 20 calls the actor method spins in `while True: pass`. Only worker mode was tested. Own-GIL and isolated contexts were not tested. `ptrace_scope` was `1`.

These stacks predate the node-wide actor worker pool in erlang-python `0f833ae`. Current bounded actors lease workers and run bodies on worker-local loops. The context ownership and interrupt observations below are historical, not a verification of pooled actor interruption.

## What works today

| Goal | Tool | Result |
| --- | --- | --- |
| Python stacks for every thread | `_remote_debugging.RemoteUnwinder(pid, all_threads=True)` with `sudo` | Works. The spinning thread shows `Orders.place` at `main.py:14` under `Container.dispatch` |
| asyncio task tree | `sudo python -m asyncio ps <pid>` and `pstree <pid>` | Works, but only for the managed entry loop: `Task-1` awaiting `TaskGroup.__aexit__`. Worker tasks are not listed |
| Stacks, tasks, and app state without root | BEAM distribution, then Python run in a new worker context on the target node | Works. Shows every thread's stack, the spinning actor, and all asyncio tasks of the entry loop, including `worker-0` to `worker-2` |
| Interactive `pdb` | `sudo python -m pdb -p <pid>` | Works with [erlang-python#9](https://github.com/h0rv/erlang-python/pull/9); failed before with `RuntimeError: Can't find the main thread in the remote process` |
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

### Stock Python debuggers

CPython's remote debuggers attach to the interpreter's main thread. Only `Py_RunMain` records that thread, so in an embedded interpreter `pdb -p` first failed with "Can't find the main thread". erlang-python now records the thread that runs a context's event loop while the loop runs ([erlang-python#9](https://github.com/h0rv/erlang-python/pull/9)). In PyBeam that thread runs the entry loop and, in worker mode, actor methods.

With that change, these commands attach to the PID that `pybeam ps` prints:

```sh
sudo python -m pdb -p PID                         # 3.14+: breakpoints, stepping, inspection
sudo python -m profiling.sampling attach -d 5 PID  # 3.15+: sampling profile
sudo python -m profiling.sampling dump PID         # 3.15+: one stack dump
```

- Run the same Python minor version as the app. A 3.15 client refuses a 3.14 target.
- They need `ptrace` rights: `sudo`, or `kernel.yama.ptrace_scope` set to 0.
- On the probe app, `pdb -p` stopped in the spinning `Orders.place` frame and `p self.seen` returned its state. `sys.remote_exec` ran on the loop thread and listed every task. The 3.15 profiler took 1000 samples per second with no errors. Without the erlang-python change, the profiler got no samples and `pdb -p` could not attach.
- While `pdb` is stopped, the loop does not run. Every task on that loop, including actor methods that run on it, waits until you continue.
- PyBeam pins CPython 3.14.10, so the 3.15 run used a local 3.15.2 build of the runtime. Own-GIL and isolated contexts are not marked. These tools reach only the main interpreter.

### Why interrupt missed the actor

`py_context:interrupt/1` raises `KeyboardInterrupt` in the thread that runs the context's current call. The actor's context was waiting on an async result while the method ran on a different thread, so the exception went to the wait, not to the spin. This is an inference from the observed stacks; the erlang-python source was not traced.

## The `pybeam debug` spike

The spike wraps this path in three commands. BEAM distribution stays the transport and erlang-python contexts run the code; there is no separate debug server.

```sh
pybeam run --debug main.py   # terminal 1
pybeam ps                    # terminal 2: list debuggable apps
pybeam debug                 # attach; pick an app when several run
```

- `pybeam run --debug` names the VM `<file>-<pid>@127.0.0.1`, binds distribution and EPMD to loopback, and writes a `0600` record to `$XDG_RUNTIME_DIR/pybeam-<uid>/`. The record is removed on exit; `pybeam ps` also drops records whose process is gone. `launch()` still rejects configured distribution.
- `pybeam debug [name]` starts a hidden client node from the same runtime. A process it spawns on the target owns one fresh worker context and stops it when the client exits.
- The menu offers `s`, every Python thread's stack plus `asyncio.format_call_graph` for each task on the entry loop, and `p`, a `code.InteractiveConsole` that evaluates input in that context against a copy of the entry file's globals. Expressions print their `repr`; `print` output goes to the app's own stdout.
- Authentication is the user's `~/.erlang.cookie`, which Erlang creates readable only by its owner. Any process of that user can already attach.

On the probe app, `s` listed the spinning `Orders.place` frame and all five tasks, and the shell returned `Orders`, `Orders.place`, and statement results. Raw `erl -remsh` remains the route for [OTP inspection](observability.md).

## Gaps before this is a feature

- erlang-python needs a supported way to run code on a context whose loop is running (for example `py_context:submit/4` to a debug entry point), and to list each context's event loop. Without it, `stacks` sees only threads and the entry loop.
- Interrupting a spinning actor needs the interrupt to reach the thread that runs the actor method.
- Own-GIL and isolated contexts have their own interpreters. A debug context in the main interpreter cannot see their state; each needs evaluation inside that interpreter.
- `pdb -p` works only from the same machine with `ptrace` rights. Remote `pdb` over distribution would need its input and output sent through the debug client.
- The debug shell runs arbitrary code inside the app. Anyone with the cookie controls the node, and cookies do not encrypt traffic. Keep it loopback-only and opt-in.
- Running debug code holds the GIL in worker mode and can slow or block the app. A slow `repr` delays every worker context.
- If an EPMD daemon already runs on all interfaces, the loopback setting does not move it.
- Thread names are bare idents: erlang-python's context threads are not `threading.Thread` objects.
- Untested: free-threaded builds, macOS, Hornbeam apps, and cleanup when the client disconnects mid-command.
