# Inspect and stop runaway work

```console
uv run --extra fastapi python -m examples.soul.main
```

Open <http://127.0.0.1:8765> and choose **Run walkthrough**. The chart measures
HTTP latency as a calculation enters a non-yielding loop, is found, is stopped,
and is replaced for new jobs. Event times line up with the chart; inspection
shows only the calculation's own source frame and sampled CPU time.

Two clients send requests to the FastAPI app throughout the sequence. Completion
requires zero failed requests, unchanged serving-process identity, and a request
counter matching every response. Nothing is retried. Light and dark modes follow
your system setting. Stop and rerun the command to replay with fresh measurements.

The bug is in `entries.py`: decrementing a negative upper bound never reaches
zero. `TaskRef.inspect()` reveals the running loop without asking it to cooperate.
`TaskRef.terminate()` stops its isolated child and waits for the task's settled
outcome. The replacement rejects negative bounds and computes valid sums directly.

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
