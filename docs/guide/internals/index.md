# How PyBEAM works

This section explains the runtime under the API, for people changing PyBEAM or debugging how it behaves. You do not need it to use the library.

PyBEAM embeds CPython in the BEAM, Erlang's virtual machine, through [erlang-python](https://github.com/h0rv/erlang-python). The BEAM provides process identity, mailboxes, supervision and scheduling; erlang-python owns how Python code runs inside it; [Hornbeam](https://github.com/h0rv/hornbeam) provides the counter, cache and pub/sub services and serves ASGI apps. For an introduction to the BEAM, watch Saša Jurić's [The Soul of Erlang and Elixir](https://www.youtube.com/watch?v=JvBT4XBdoUE) or read the Erlang documentation on [processes](https://www.erlang.org/doc/system/ref_man_processes.html).

## The stack

![One OS process runs the BEAM VM with one scheduler and many BEAM processes. Worker contexts, each with its own OS thread, run in the main Python interpreter; actor calls lease a worker from a node-wide pool and run on that worker's own loop. Isolated tasks run in a child OS process outside the VM.](../assets/stack.svg)

| Layer | What it is | Measured cost |
| --- | --- | --- |
| OS process | The BEAM VM that `pybeam.run` starts. Everything below shares it. | About 85 MiB at start |
| BEAM scheduler | Runs BEAM processes preemptively. The managed VM starts one (`+S 1:1`), so Python parallelism never comes from BEAM schedulers. | |
| BEAM process | The identity, mailbox, links and supervisor of an actor or task. It holds no Python state. | 2.7 KB |
| Python interpreter | The main CPython interpreter, embedded in the VM. A native crash in it ends the VM. | |
| GIL | When enabled, one thread at a time runs Python. Free-threaded builds with the GIL disabled can run Python on several threads at once. | |
| Worker context | An erlang-python context: one OS thread that runs Python in the main interpreter. Actors lease workers from a node-wide pool, not one context per actor. | 1.1–1.2 ms to create a context; pool size, not actor count, determines worker thread count |
| Shared event loop | One asyncio loop thread, erlang-python's `ErlangEventLoop`, which takes I/O readiness and timers from the BEAM. Shared-loop tasks use it; actor calls use worker-local loops instead. | |
| Dedicated loop | A worker task started with `dedicated_loop=True` runs on its context's own loop and thread. | |
| Isolated context | A child OS process with its own interpreter. Requests and results cross as bytes. | 61–132 ms to create, 21–27 MiB |

Distinguish these layers when you reason about a change. A BEAM process is not an interpreter, a thread or an OS process, and a free-threaded build does not make one event loop run on more than one CPU. The [execution model](../../research/execution-model.md) covers each boundary, along with nodes and storage.

## How a call reaches Python

![A BEAM process sends a call to a mailbox. A Python context takes the call. Supported contexts run as worker or isolated. The result returns as a message, or a crash signal goes to the supervisor.](../assets/call-path.svg)

1. Each actor has a BEAM process and mailbox. It handles one call at a time, leasing a worker for construction and each call. The group's `max_children` caps actors, not worker contexts; the node-wide pool bounds active actor workers and queues checkouts at its cap.
2. Tasks own their execution context. `TaskLimits` caps task ownership slots; starting a task when those slots are full raises `Overloaded`. The context's mode decides which interpreter runs the code and what a crash takes down.
3. The result comes back as a message. If an actor crashes, its group starts a replacement with fresh state; it does not replay the call. Tasks are temporary children: their supervisor does not restart them.

## Where parallel Python comes from

![Shared-loop tasks run on one loop thread. Actor calls lease pooled workers with separate threads and loops, so one blocking actor call does not block calls using other workers.](../assets/actor-loop.svg)

- **Actor calls** run on pooled workers, each with its own loop. Blocking operations that release the GIL, such as `time.sleep`, overlap on different workers on both builds. Pure-Python CPU work can overlap on 3.14t with the GIL disabled; the GIL serializes it on conventional builds.
- **Shared-loop tasks** share one loop thread. CPU-bound or blocking code in one task stalls the other tasks on that loop, even on a free-threaded build.
- **Dedicated-loop tasks** run on their own thread, so they overlap on 3.14t and take turns on 3.14.
- **Isolated tasks** run in separate OS processes, so they overlap on either build.

If an actor's preferred worker is busy, its next call takes another worker immediately. Do not keep loop-bound objects across calls. A background asyncio task started during a call is not guaranteed to progress while that worker is idle.

[Measurements](measurements.md) records the earlier execution-mode probes, including the pre-pool actor path; those actor costs and timings do not describe the pooled runtime. [What to watch for](watch-for.md) covers the failure modes.

## Own-GIL subinterpreters are unsupported

erlang-python also offers own-GIL contexts, one subinterpreter per context. PyBEAM does not support them: pydantic-core, uvloop, httptools, SQLAlchemy, numpy, pandas, cryptography, lxml, msgspec, grpcio, greenlet, ujson, PyYAML and orjson all refuse to load in a subinterpreter on 3.14 and 3.14t. A subinterpreter also isolates Python state, not native crashes. See the [package compatibility results](../../research/execution-modes.md#unsupported-own-gil-subinterpreters).

## Further reading

- [Execution model](../../research/execution-model.md): each boundary from BEAM process to node.
- [Execution modes](../../research/execution-modes.md): the mode probes and their method.
- [Actor capacity](../../research/actor-capacity.md): actor cost, call timing and state movement.
- [Worker design](../../research/worker-design.md): the broader proposal, including durable state migration and wait-then-fallback routing. The current pool reuses in-memory actor objects and falls back immediately when a preferred worker is busy.
