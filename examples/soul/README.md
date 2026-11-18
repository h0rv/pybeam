# Inspect and stop runaway work

```console
uv run --extra fastapi python -m examples.soul.main
```

Two HTTP clients send requests to a FastAPI app while a calculation enters a
non-yielding Python loop. The walkthrough samples that child's Python stacks and
CPU time, stops it, then selects a corrected calculation for new jobs. The HTTP
server and its stateful request counter stay running. Response counts, latency,
child CPU time, and actor identity come from the current run; the program fails
if traffic stops, requests fail, or the serving actor restarts.

The bug is in `entries.py`: decrementing a negative upper bound never reaches
zero. `TaskRef.inspect()` reveals the running loop without asking it to cooperate.
`TaskRef.terminate()` stops its isolated child and waits for the task's settled
outcome. The replacement rejects negative bounds and computes valid sums directly.
No request or task is automatically retried.

This follows the inspect → stop → repair sequence in Saša Jurić's
[The Soul of Erlang and Elixir](https://www.youtube.com/watch?v=JvBT4XBdoUE),
not a crash/restart comparison.

## Boundaries

- The calculation uses an isolated OS process. This is not preemptive scheduling
  of Python code inside a shared interpreter.
- Inspection samples Python threads, not native stacks or BEAM reductions.
  A native extension holding the child's GIL can prevent inspection. The reader
  can inspect a busy Python loop, but inspection has a five-second deadline.
- Forced termination does not run Python finalizers or undo external side effects.
  A termination deadline means the outcome is uncertain, not that exit succeeded.
- The repair explicitly selects a different, already available function for new
  jobs. It does not patch running frames or demonstrate OTP hot-code upgrades.
- Request counts and actor state are in memory, not durable storage. The clients
  exercise a local server; these timings are observations, not benchmarks or an
  availability guarantee under every load.

The runnable example needs runtime assets containing isolated-task inspection
and termination; an older installed runtime does not provide those operations.
For contributor setup, see [Development](../../docs/development.md).
