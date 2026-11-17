# Runtime contracts and gaps

Historical research note: own-GIL subinterpreter mode is unsupported because common native extensions do not load in it; use free-threaded 3.14t workers or isolated OS processes instead. See the [compatibility results](execution-modes.md#unsupported-own-gil-subinterpreters).

## Evidence boundary

This is source-only research, not runtime certification. E means `erlang-python@aa8f021`; H means `hornbeam@1217ae8`. Paths below are relative to those sibling repositories. Both checkouts matched these revisions at inspection. Concurrent service-binding and WebSocket changes are excluded. Extensions checkpoint `a788a41`, current `AGENTS.md` and `README.md`, and the archived `/tmp/hornbeam-mvp-notes.mlpmlD/{WORKLOG.md,docs/architecture.md}` provide context, not runtime guarantees. No builds or runtime experiments were run against shared outputs.

## Identity and state

Existing execution APIs are not Python actors. A `py_context` PID owns execution on a dedicated thread. The calling Erlang process owns a separate environment through `py:get_local_env/1`. Its dictionary is keyed by interpreter ID, not context PID, despite E `docs/process-bound-envs.md`. Worker contexts share the main interpreter, including imported module state. Own-GIL contexts have separate interpreters, not separate OS processes. Neither contains a native crash. See E `src/{py,py_context,py_context_embedded}.erl`.

A supervised Erlang `gen_server` retaining its environment already supports stateful actor candidates. E `docs/process-bound-envs.md` demonstrates this; `test/py_actor_SUITE.erl` contains tests, not results reproduced here. Restarting the owner gives a fresh namespace, not durable state or a reset of shared modules. Resource release need not mean immediate memory reclamation. Isolated mode instead has one child namespace per context and ignores environment references. Child restart loses Python state. See E `src/py_isolated.erl` and `docs/isolated.md`.

## Mailboxes and workers

BEAM mailboxes belong to Erlang processes, not Python objects or asyncio tasks. Callback handlers can be separate processes, so callback `self()` is not an actor identity. No bounded actor mailbox follows from an environment. Context requests and task results use ordinary messages without admission bounds. See E `src/{py_context_embedded,py_event_loop}.erl`.

Existing `py_channel` queues are separate native resources. Their default is unlimited. `max_size` bounds serialized bytes and sends return `busy`; there is one registered asynchronous or synchronous waiter. The creator PID is recorded without a death monitor in this implementation. Explicit closure is necessary. Receive cancellation unregisters a waiter but does not replay consumed messages. See E `src/py_channel.erl`, `c_src/py_channel.c`, and `priv/_erlang_impl/_channel.py`.

Standalone services already have `start_loop`, `submit`, `submit_await`, and `stop_loop`. Submission targets importable module functions, not the exec namespace. Results go to the submitter's mailbox. Await timeout only stops waiting. Worker mode shares one interpreter loop; own-GIL allows separate loops. Loop owners are monitored. These need neither Hornbeam nor ASGI. See E `docs/workers.md`, `src/{py_context,py_context_embedded,py_event_loop}.erl`.

H `src/hornbeam_tasks.erl` also exposes handler-based background tasks, not durable jobs. Its advertised execution timeout is ignored. Workers are linked to a manager that does not trap exits; cancellation can therefore kill that manager. Task restart and reliable cancellation are not established by its supervision comments.

## Failure and distribution

Embedded synchronous context timeouts interrupt the context, potentially another request. Interruption waits for bytecode and cannot stop a stuck native call. Isolated mode uses signals and a kill backstop with a restart budget, but lacks native channels and object references. It is not a security sandbox. Context children under E `src/py_context_sup.erl` are `temporary`; application supervisors must supply restart policy.

E `src/py_callback.erl` supplies local registered-name lookup. H `src/hornbeam_dist.erl` supplies node listing/connect/ping and Erlang RPC, not actor discovery. Calls wrap results; casts acknowledge no completion. Timeout/disconnection gives no proof that side effects did not happen. E `docs/distributed.md` suggests separate RPC create/await calls, but results target the submitting RPC process, not a later caller. Treat that recipe as unverified, not transparent remote task ownership.

## Ownership decisions

E already supplies ETS state/counters and process-scoped shared dictionaries. Its `src/py_state.erl` missing-key increment fallback can lose a racing increment. H `src/hornbeam_state.erl` retries the atomic update. Both stores are node-local and volatile.

H `src/{hornbeam_services,hornbeam_pubsub}.erl` adds bounded byte cache and managed subscriptions with owner monitoring and overflow closure. Ordinary pubsub sends remain unbounded. Publish counts membership, not receipt. The copied H `priv/hornbeam/` package is a checkpoint prototype, not verified upstream API.

PyBeam should own typed handles and explicit lifecycle ergonomics. erlang-python owns identity, cancellation, counter correctness and mailbox admission contracts. Hornbeam owns service bindings and subscription cleanup. Unknowns remain cross-node resource transport, restart cleanup under load, and Python-facing supervised spawn contracts. Do not add another scheduler or supervision framework.
