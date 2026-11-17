# Inspect the BEAM before adding tools

Use the Erlang shell first. It already exposes process ownership and cleanup without another service or dependency.

## Evidence boundary

This research read current `AGENTS.md` and `README.md`. Archived architecture and work logs are historical context, not contracts. Source claims below use Hornbeam `1217ae8`, erlang-python `aa8f021`, and extensions `a788a41`. Concurrent Python binding and WebSocket changes are outside this evidence. No runtime was rebuilt or shared process stopped.

Installed mise OTP reports OTP 29 and ERTS 17.1.1. `observer`, `wx`, and `dbg` exist. `observer_cli` and `recon` are absent. The wx native driver exists, but no GUI was opened. `rebar3 help shell` confirms `--sname`, `--setcookie`, and `--start-clean`.

## Reproduced two-terminal inspection

Run both terminals from the repository root. These commands use disposable nodes, not Hornbeam. The remote shell sequence was reproduced using piped shell input. Actual PIDs vary.

Terminal one:

```sh
mise exec -- erl +S 2 -noshell -sname pybeam_probe_target \
  -setcookie pybeam_local_probe \
  -kernel inet_dist_use_interface '{127,0,0,1}' \
  -eval 'timer:sleep(infinity).'
```

Terminal two:

```sh
mise exec -- erl +S 2 -sname pybeam_probe_inspect \
  -setcookie pybeam_local_probe \
  -kernel inet_dist_use_interface '{127,0,0,1}' \
  -remsh "pybeam_probe_target@$(hostname -s)"
```

The prompt identifies the target node. Enter:

```erlang
node().
P = spawn(fun() ->
    ets:new(pybeam_probe, [named_table]),
    receive stop -> ok end
end).
register(pybeam_probe_owner, P).
M = monitor(process, P).
link(P).
P ! queued.
process_info(P, [registered_name, links, monitors,
                 monitored_by, message_queue_len]).
supervisor:which_children(kernel_sup).
ets:info(pybeam_probe, owner).
unlink(P).
exit(P, kill).
receive {Tag, M, process, P, killed} -> {Tag, cleaned}
after 2000 -> timeout end.
process_info(P).
whereis(pybeam_probe_owner).
ets:info(pybeam_probe).
halt().
```

The process had one queued message, a shell link, and a shell monitor. ETS ownership matched its PID. After the controlled failure, the monitor delivered `DOWN`; process, registered name, and table queries returned `undefined`. Unlinking prevents the deliberate failure from killing the shell. `halt()` stops only this disposable target. Exit the remaining inspector VM separately.

## Apply to the existing runtimes

These are source-based suggestions, not reproduced runtime startup instructions. Hornbeam already documents `rebar3 shell`; its configuration starts Hornbeam. Prefer an already built runtime and the existing launcher. For a private development launch, Erlang's `ERL_FLAGS` can supply the same distribution flags to `mise run serve`. That launch path was not tested here. Do not attach to or restart someone else's server.

Once attached to your own named runtime:

```erlang
supervisor:which_children(hornbeam_sup).
supervisor:which_children(erlang_python_sup).
supervisor:which_children(py_context_sup).
P = whereis(hornbeam_state).
process_info(P, [registered_name, links, monitors,
                 monitored_by, message_queue_len]).
[{T, ets:info(T, owner)} || T <- ets:all()].
```

These are existing OTP APIs, not a PyBEAM inspection API. Check `whereis/1` before querying absent services. At the checkpoint, `hornbeam_state` owns `hornbeam_state_table` without an heir. Hornbeam uses `one_for_one`; erlang-python uses `one_for_all`, while context children are temporary. Restart does not imply state recovery. WebSocket and native resource cleanup remain unverified by this probe.

## Optional tools and limits

A separate isolated VM reproduced a process exit trace with `erlang:trace(P,true,[send,procs])`. Disable tracing while the process lives with `erlang:trace(P,false,[all])`. Disabling after death raised `badarg`. Keep tracing narrow and brief; events can expose payloads and overload the tracer. OTP `dbg` is available but was not exercised.

`observer:start().` requires working wx and a desktop session. `observer_cli:start().` requires separately installed observer_cli; it is not currently available. Neither UI was tested or added.

Distribution requires matching cookies, compatible node naming, resolvable hostnames, EPMD connectivity, and reachable distribution ports. The example binds distribution to loopback; EPMD has its own binding. Cookies grant powerful node access and do not encrypt traffic. The public sample cookie is only for this disposable local exercise. Protect actual cookies and restrict network access; do not expose distribution to untrusted networks.

Remote inspection provides no replication, migration, failover, or native-crash isolation. A native extension crash can still terminate the VM.
