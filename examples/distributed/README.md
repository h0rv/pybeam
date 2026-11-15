# Local distribution

From a prepared source checkout, run:

```sh
.venv/bin/python -m examples.distributed.demo
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

## Source development

Use conventional CPython 3.14.10 and OTP 29.1.2. Build the paired native checkout
with its default rebar profile, a matching `PYTHON_CONFIG`, and
`CMAKE_BUILD_PARALLEL_LEVEL=1`. Do not build optional dependencies through this
example. Set `PYBEAM_DEVELOPMENT_RUNTIME` to that checkout's `_build/default` if it
is not the sibling `native-local-distribution` checkout. Prepare this checkout's
editable Python environment separately. This is not a clean wheel installation
claim, and no manual Erlang commands are needed when running the prepared demo.

Run `mise run test:distributed` for the explicit finite tests. Ordinary core tests
do not start these VMs. The fixed `double` definition is an example operation,
not an arbitrary remote Python or Erlang execution API. Remote PIDs remain in
Erlang. Their text in the result is data, not a local `erlang.Pid` handle.
