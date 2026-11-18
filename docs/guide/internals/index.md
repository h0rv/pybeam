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

Next: [How a call reaches Python](calls.md).
