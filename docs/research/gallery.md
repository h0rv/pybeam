# A small example gallery

Start with three exercises in one collection: separate state, the existing shared counter, and a standalone worker. Add failure inspection as a variant. Gate clustering on evidence. Frameworks are alternate entry points. These are proposals, not MVP requirements.

## Evidence

Evidence comes from [runtime gaps](runtime-gaps.md), [API research](api-design.md), and [observability](observability.md). Only disposable OTP inspection was reproduced. Checkpoints are erlang-python `aa8f021`, Hornbeam `1217ae8`, and extensions `a788a41`. Concurrent binding and WebSocket work is excluded. Historical architecture and work logs are context, not contracts.

Source-inspected means read, not executed. Proposed exercises lack runtime evidence. Blocked outcomes cannot yet be supported.

| Exercise | Prerequisites | Actual runtime capability | Expected observation and limitation | Status |
| --- | --- | --- | --- | --- |
| 1. Two Python-backed processes with separate state | Two Erlang owners using process-bound environments in conventional worker mode. | Each owner can retain a Python namespace. Worker contexts share an interpreter and imported modules. | Independent namespace counters should differ. This does not establish isolated modules, interpreters, or native crashes. | Proposed. The environment mechanism is source-inspected. |
| 2. Shared state and pub/sub | Existing counter example, Hornbeam services, two independent WebSocket clients. | Node-local ETS counters and managed subscriptions exist. Publication counts membership, not receipt. | Both clients should reload the same state after invalidation. Update and publish are separate operations. Cleanup acceptance was not verified at the inspected checkpoint. See the repository README for current results. | Source-inspected. Full runtime acceptance is not verified. |
| 3. Standalone background worker | erlang-python application and an importable async function. No Hornbeam or ASGI. | `start_loop`, `submit`, `submit_await`, and `stop_loop` exist. Results belong to the submitter. | Observe completion and explicit shutdown. Await timeout does not cancel work. This is neither a durable queue nor an actor API. | Proposed. Worker primitives are source-inspected. |
| Failure variant of 1 and 3 | An existing OTP supervisor with an explicit restart policy. | Context children are temporary. Owner death releases its environment. | Observe DOWN and resource ownership. A replacement owner starts fresh state. Python-facing supervised spawning and cleanup under load remain unknown. | Proposed. Generic OTP failure inspection is existing verified behavior. |
| Two-node variant | Private named nodes, matching cookies, reachable distribution ports. | Node connection and Erlang RPC exist. | Observe connection and explicit RPC first. No replicated ETS, transparent actor calls, or transferable native handles are established. | Blocked for a shared-state or actor demonstration. Remote-shell attachment is existing verified behavior. |
| Framework variants of 2 | Optional FastAPI, Litestar, or PyView dependencies and existing example modules. | Adapters can use the same service boundary and explicit subscriptions. | Compare lifecycle integration, not new service implementations. Framework compatibility still requires runtime tests. | Source-inspected, not runtime-certified. |

Omit actor decorators, automatic retries, and failover demonstrations. The most useful next experiment is two Erlang owners executing the same Python function with distinct environment state, followed by one controlled owner failure. Record surviving state and remaining resources before proposing an actor abstraction.
