# Connect PyBEAM to an existing BEAM application

Should PyBEAM use an existing Erlang or Elixir application's runtime instead of
letting `pybeam.run` own a VM? Three shapes have different ownership boundaries:

- **Embed:** run PyBEAM inside the application's existing VM.
- **Join:** run a separate PyBEAM peer worker that the application passes work to.
- **VM-free client:** connect from Python without starting a local VM.

**Support Join first.** It needs the smallest integration change, keeps PyBEAM in
charge of its own runtime, and fits a Python-first application. Embed is a useful
advanced polyglot path. A VM-free distribution client is not recommended for core.

## Rerun the probes

From the repository root on Linux, prepare the locked native build:

```sh
mise trust && mise install
mise exec -- uv run --locked python tools/build_native.py
```

Then run each shape independently:

```sh
bash docs/research/probes/attach/run.sh
bash docs/research/probes/attach/run_join.sh
```

These are native development probes, not public startup APIs. They use local
named nodes and a fixed test cookie; do not run them against a real cluster.
The Join probe generates its development-runtime configuration under `_build`.

## Embed

Rerun every result in this table with
`bash docs/research/probes/attach/run.sh`.

| Result | Cause and source |
| --- | --- |
| Actors return 12 then 15; a task returns 42. Both ownership scopes close. | Public wrappers open native groups directly, without the managed loader: [actor groups](../../src/pybeam/actors/groups.py#L44), [tasks](../../src/pybeam/tasks.py#L404). The host starts `erlang_python` and the context driver: [probe](probes/attach/host.eval). |
| A host message reaches Python through a bounded channel; Python replies to the host's local PID. | Existing channel and PID bindings work inside one VM: [probe](probes/attach/attach_probe.py#L60). This is not a general Python mailbox API. |
| The loader and optional-application guard reject the session. | [Loader startup](../../src/pybeam/runtime/loader.py#L118) requires `PYBEAM_BOOTSTRAP`; [the guard](../../src/pybeam/runtime/loader.py#L46) requires a managed entry task. |
| Hornbeam counters fail before host setup, then return 1 after setup. | The host must start the services and register their callback: [probe](probes/attach/host.eval#L21). `Beam.connect` delegates to Hornbeam: [client](../../src/pybeam/integrations/hornbeam/client.py#L75). |
| The host survives Python completion and stopping `erlang_python`. | The probe owns shutdown; it never calls `py_launch`: [host lifecycle](probes/attach/host.eval#L29). |

The missing supported boundary is a **host-owned entry/lifecycle API**: a
supervised child the host starts, with no file re-execution, VM-wide signal
ownership, or VM halt. Optional services such as Hornbeam need explicit host
startup. Keep this advanced path separate from `pybeam.run`, whose
[entrypoint](../../src/pybeam/runtime/entrypoint.py#L19) launches an owned VM when
no managed loader session exists.

## Join

Rerun every result in this table with
`bash docs/research/probes/attach/run_join.sh`.

| Result | Cause and source |
| --- | --- |
| A plain host with no Python runtime loaded receives actor/task results through `erpc:call`. | A scratch Erlang facade submits an async Python function on the worker: [dispatch](probes/attach/join_probe.erl#L13), [Python work](probes/attach/join_work.py#L7). Connected nodes are peers, not master and subordinate runtimes. |
| Direct `erlang.send` to a remote PID fails; Python receives `None` instead of a PID wrapper. | Native conversion handles only local PIDs, then falls back to `None`; the send binding requires a PID wrapper. See the runtime source references below. |
| Direct `erlang.send` to `{Name, Node}` also fails. | The same binding rejects tuple destinations. An explicit scratch Erlang callback retains the remote PID and successfully forwards the push: [bridge](probes/attach/join_probe.erl#L13). |
| Killing the worker after Python announces execution produces `{erpc,noconnection}` and `nodedown`; the host survives. | The probe waits for the start message, kills only the worker, and observes both RPC failure and the node monitor: [failure probe](probes/attach/join_probe.erl#L35). |
| A waiting `pybeam.run` VM accepts a host connection and runs the same work. | `ERL_FLAGS` supplies a node name and cookie. The host loads the scratch facade and calls it while the managed entry waits: [managed probe](probes/attach/join_probe.erl#L64). |

The managed probe observes `PYBEAM_DISTRIBUTION` absent and
`dist_auto_connect` unset. The existing two-node
[`Distribution`](../../src/pybeam/runtime/config.py#L49) is a different shape:
its [launcher](../../src/pybeam/runtime/cluster.py#L240) sets
`dist_auto_connect=never`, and the runtime restricts allowed nodes only when
`PYBEAM_DISTRIBUTION` is configured. Those restrictions do not execute in this
probe. The existing [DNS discovery](../../src/pybeam/runtime/discovery.py#L12)
constructs fixed-format peer names; it is not an arbitrary existing-node join
option.

Add an explicit **Join configuration** for node name, cookie source, peers, and
bounded connection attempts, separate from `Distribution`. PyBEAM should own
only its worker runtime, not the existing cluster's lifecycle.

Also fix the erlang-python boundary so remote PIDs survive conversion and Python
can send to remote PIDs or `{Name, Node}` through an **Erlang-owned send path**.
Do not treat the probe's single-destination callback as a production transport.

For managed shutdown, signal the owning Python launcher. Signaling only its VM
produced an entry/VM status disagreement; the launcher
[checks those statuses](../../src/pybeam/runtime/process.py#L240). The final
probe signals the launcher and observes exit 143.

### Runtime source references

After the native build, inspect these files under
`_build/hornbeam/lib/erlang_python/`:

- `c_src/py_convert.c:725–733,821–822`: local-PID conversion and unknown-term fallback.
- `c_src/py_callback.c:2000–2023`: PID-only validation and native send.
- `src/py_distribution.erl:7–18`: environment gate, `dist_auto_connect` check,
  and `net_kernel:allow`.

## VM-free Python client

These are source-review findings, not executed interoperability results.

| Finding | Source and how to recheck |
| --- | --- |
| PyBEAM's debug attach is not VM-free. It starts a hidden local VM and creates a Python context on the target. | Read [`connect`](../../src/pybeam/runtime/attach.py#L183) and [the client program](../../src/pybeam/runtime/attach.py#L25). |
| Pyrlang is the only candidate considered here for a Python distribution implementation; it is untested with PyBEAM. | Review [Pyrlang's protocol support](https://github.com/Pyrlang/Pyrlang#readme) and [node implementation](https://github.com/Pyrlang/Pyrlang/blob/master/pyrlang/node.py). Verify package identity before installing; the PyPI name can refer to a different project. |

A distribution client does not provide the native runtime needed by PyBEAM
actors and tasks. Do not add a second distribution implementation to core.
For Python clients that must have no VM, expose an application-owned HTTP API or
queue; any worker execution still needs its runtime and explicit failure policy.

## Trust and failure boundaries

- A shared cookie grants full code execution on every connected node. It is a
  cluster trust credential, not per-operation authorization.
- Plain distribution is unencrypted. Cross-machine use needs
  [TLS distribution](https://www.erlang.org/doc/apps/ssl/ssl_distribution.html).
- Node loss yields `{erpc,noconnection}` and `nodedown`, but completion of effects
  is unknown. No automatic retry, durable work, or durable state is implied.
  Rerun the node-loss observation with
  `bash docs/research/probes/attach/run_join.sh`.

## Open questions

Not tested: Elixir/Mix releases, cross-machine networking and TLS, reconnects,
partitions, or load limits. The probes do not establish production readiness.
Decide who owns admission, cancellation, and reconnect policy before exposing
Join as a public API.
