# Documentation plan

Use this navigation order: Quickstart → runtime model → primitives → BEAM inspection → advanced configuration. Link [gallery](gallery.md), [execution model](execution-model.md), [runtime gaps](runtime-gaps.md), [API research](api-design.md), [observability](observability.md), and [live debugging](live-debugging.md) as research, not released contracts.

## Quickstart

Explain shared state versus Python globals. Require sibling `hornbeam`, `erlang-python`, and `erlang-ws` checkouts. Current README and mise commands from `pybeam`:

```sh
mise install
mise run setup
mise run check
mise run test
mise run integration
mise run serve
```

Open `http://127.0.0.1:8642/` in two browser sessions. Change the counter and expect both views to reload shared state after invalidation. Browser observation does not replace the independent WebSocket integration gate. These commands were read, not run here. Setup builds dependencies. This research did not verify slow-consumer cleanup. See the repository README for current test results.

## Runtime model

Show who owns each resource before teaching abstractions. erlang-python owns execution and environments. Hornbeam owns current shared services and ASGI. PyBeam owns typed handles and framework lifecycle integration. A Python object is not a BEAM process. Worker contexts share imported modules. ETS state is volatile and node-local. Owner restart does not restore state, and embedded native failure can terminate the VM.

## Primitives

Start with typed counter and byte-store operations. Then show an explicitly owned subscription using `async with`. Explain invalidation followed by a fresh read, overflow, and closure. Publication does not acknowledge delivery. Cancellation of a wait does not establish cancellation of execution. A timed out increment may already have happened.

Label checkpoint service signatures as source-inspected prototypes until current bindings pass runtime tests. Put optional framework lifespan and dependency examples after the core contract. Do not present the actor typing probes as executable remote calls.

## BEAM inspection

Link the reproduced two-terminal commands in [observability](observability.md). Teach `process_info`, `supervisor:which_children`, and ETS ownership before optional Observer. Explain why a mailbox count and a DOWN message help distinguish pending work from owner failure. Only kill disposable processes. Runtime attachment through the current launcher remains untested.

## Advanced configuration

Document Python 3.14 conventional worker mode first. Separate other interpreter modes, distributed node configuration, and restart policies as experiments. Explain cookie access and network restrictions before remote commands. Do not imply distribution provides state replication.

Keep environment internals and native channel details in optional source-linked material. Omit a new dashboard and speculative spawn API. Next, reproduce the separate-owner failure experiment from the gallery and turn its exact commands and observations into the first standalone runtime tutorial.
