# Local distribution

With a compatible runtime wheel installed, copy this directory to
`examples/distributed` in your project and run:

```sh
python -m examples.distributed.demo
```

You can also use `mise run example:distributed`. Neither command builds a runtime.
Both commands start two named OTP VMs and a foreground private EPMD. They print
one finite JSON result after stopping every owned process.

`config.py` declares two `PythonNode` entries, finite limits, and three private
ports. `LocalCluster` uses the existing managed launcher. Each VM waits for the
guardian to record its PID and creation time before executing the entry file.
The cookie is in a private file with mode 0600. No shared EPMD is used.
OTP also listens for private EPMD discovery on the IPv6 loopback address `::1`.
The VM distribution listeners use only `127.0.0.1`.

A sends data through OTP `erpc` to the fixed handler on B. B constructs its own
`TaskSupervisor` and `TaskFunction`, then registers the fixed `double` definition. Python callables and native handles never
cross nodes. The result includes the handler identities and a temporary task
PID checked inside B. B also calls A through OTP for the receipt acknowledgment.

Reservations cover recipient count and encoded request bytes before work enters
the application queue. Accepted jobs remain charged until body cleanup and an
explicit result acknowledgment. Polling has no persistent server waiters.
Abandoning an observer does not cancel work. Finished unobserved results consume
slots. At most 64 message IDs are accepted during one recipient incarnation.
The event history holds 32 entries and reports dropped entries. Control requests
and trusted OTP transport are outside the application byte budget.

The cluster lifetime is at most 120 seconds, with one permitted restart per node.
The example stops B gracefully while a task is running. A observes node down and
reports uncertainty. Restarting B creates empty state and a new incarnation.
A must explicitly join again and submit a new request. There is no replay or
durability. These limits do not bound network decoding or total process memory.

A host can pass a `GuardianEndpoint` through `LocalCluster(..., guardian=endpoint)`
when an existing outer guardian implements the registration acknowledgment
contract. The cluster borrows that guardian and does not stop it. The inner
guardian and every executable child must receive an outer acknowledgment after
their birth identity is recorded. Cancellation or a missing acknowledgment
blocks entry. The normal command does not require an outer guardian.

## Source development

Use conventional CPython 3.14.10 and OTP 29.1.2. Build the paired native checkout
with a matching `PYTHON_CONFIG` and `CMAKE_BUILD_PARALLEL_LEVEL=1`.
Do not build optional dependencies through this example. Select prepared assets
with `[tool.pybeam].development_runtime` in your `pyproject.toml`. The normal
runtime manifest is used when this setting is absent. No manual EPMD or cookie
setup is needed. The three configured ports must be free. Port reservation and
listener startup are separate OS operations, so a collision fails startup rather
than retrying.

Run `mise run test:distributed` for the explicit finite tests. Ordinary core tests
do not start these VMs. The fixed `double` definition is an example operation,
not an arbitrary remote Python or Erlang execution API. Remote PIDs remain in
Erlang. Their text in the result is data, not a local `erlang.Pid` handle.
