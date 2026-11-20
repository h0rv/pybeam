# PyBEAM

Build resilient Python services with a small runtime footprint.

PyBEAM is a runtime for async Python services on the Erlang VM. It works with existing frameworks such as [FastAPI](integrations/fastapi.md) and [Litestar](integrations/litestar.md), bringing shared state, messaging and background work into the runtime for apps that would otherwise coordinate extra workers, Redis or a task queue.

```sh
uv add python-beam
```

Built on [erlang-python](https://github.com/benoitc/erlang-python), [Hornbeam](https://github.com/benoitc/hornbeam) and [Erlang/OTP](https://www.erlang.org/).

- An [isolated call](examples/crash-containment.md) reports a native crash to its caller, which keeps running and can handle the next request.
- [Shared counters](state.md) and [pub/sub](messaging.md) live in the runtime: requests update the same values and notify current subscribers. This in-memory state lasts until the node stops.
- With persistent storage configured, Mnesia keeps [actor snapshots](actors.md#save-explicit-checkpoints) and [durable task records](durable-tasks.md) across restarts; configured replicas support node-loss recovery. Durable tasks are a prototype with at-least-once execution.
- Services share runtime resources and keep a [small memory footprint](tradeoffs.md#cost-to-run), with process startup and memory costs for isolated calls.

[Get started](index.md) · [Why PyBEAM](why.md) · [Benefits and tradeoffs](tradeoffs.md) · [Crash containment](examples/crash-containment.md)
