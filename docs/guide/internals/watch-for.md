# What to watch for

Each section names a way PyBEAM code can fail or slow down, what causes it, and what to do. [Measurements](measurements.md) has the numbers in full.

## Native crashes

A segfault or `os.abort()` in a worker context ends the whole VM: every actor, task and connection in the app. Actors and default tasks all run in worker contexts. Supervision does not help, because the supervisor dies too.

- Run code that can crash, such as an unreliable C extension, as an `isolated` task. In the probes, the other 3 of 4 contexts survived and a replacement was ready in 64–134 ms.
- An isolated task is never restarted or retried after its child process dies. `TaskCrashed` means its effects are unknown.
- Subinterpreters isolate Python state, not native crashes. To survive a crash of the whole VM, run separate nodes.

## Shared globals on 3.14t

Worker contexts share the main interpreter, so module globals, imported modules and caches are shared by every actor and task. Actor method bodies run on one loop thread and do not race each other. Code on other threads does: `asyncio.to_thread` calls, dedicated-loop tasks and contexts called directly.

- The GIL on 3.14 does not make a read-modify-write atomic, but on 3.14t threads run at the same time, so races on unsynchronized shared state show up more often.
- Threads that change the same object contend for it on 3.14t, which can erase the parallel speedup.
- An extension that does not declare free-threading support re-enables the GIL when imported; lxml did in the probes. Check `sys._is_gil_enabled()` after your imports.
- Keep mutable state inside an actor, or in the shared counter and cache.

## Blocking calls

Every actor method body, and every worker task without `dedicated_loop=True`, runs on one event loop thread. A blocking call or a CPU loop there stops all of them until it returns, on 3.14 and 3.14t alike. In the probes, 32 actors that each slept 50 ms with `time.sleep` took 1.62 s, and a 10 ms async call waited behind another actor's 300 ms `time.sleep`.

- Wrap blocking calls in `await asyncio.to_thread(...)`. A `ThreadPoolExecutor` used synchronously inside the method still blocks the loop until all its work finishes.
- Give CPU-bound tasks `dedicated_loop=True`. Actors have no such option.
- One caller loop can have at most about 8,000 calls in flight. Past that, calls raise `RuntimeError: Native callback queue full`.

## Boundary copies

Values never cross as shared Python objects. Actor arguments and results are copied as JSON, tasks use the request and result codecs you pass to `TaskFunction`, and isolated tasks exchange bytes with a child process. `TaskLimits.max_bytes` caps a task's request size.

- Copy cost grows with size. Holding a 418 KB state dict in Python cost 27–45 µs per message; pickling it through ETS and back cost 6.7–8.8 ms.
- A copy is a snapshot. Changing it does not change the original.
- Pass identifiers and keep large objects with the code that uses them.

## Context creation

A context is not free to start.

| Created | Time | Memory |
| --- | --- | --- |
| One worker context | 1.1–1.2 ms | under 0.1 MiB |
| One actor, including its context | 2.3–3.0 ms | 50–60 KiB and one OS thread |
| One isolated context | 61–132 ms | 21–27 MiB |

- Create long-lived actors at startup and reuse them rather than spawning one per request. 10,000 actors took about 24 s to start.
- Cap concurrency with `TaskLimits` and `max_children`. Starting a task when its slots are full raises `Overloaded` instead of queuing it.
