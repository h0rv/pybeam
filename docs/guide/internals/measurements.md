# Measurements

These probes compare execution costs on one machine, not production limits. Bars show measured values; thin whiskers show ranges across runs.

## Contexts

<div class="measurement-chart" tabindex="0" role="region" aria-label="Scrollable measurement chart">

![Time to create one worker or isolated context on a logarithmic axis.](../assets/measurement-create.svg)

</div>

Workers used under 0.1 MiB per context on both builds; isolated contexts used 21–22 MiB on conventional Python and 26–27 MiB on free-threaded Python. When one of four isolated contexts aborted, the other three survived and a replacement was ready in 64–134 ms; aborting a worker killed the VM.

<div class="measurement-chart" tabindex="0" role="region" aria-label="Scrollable measurement chart">

![Median empty round-trip latency for worker and isolated contexts called directly by BEAM processes.](../assets/measurement-latency.svg)

</div>

## CPU jobs

<div class="measurement-chart" tabindex="0" role="region" aria-label="Scrollable measurement chart">

![Wall time for 8 and 64 CPU jobs in worker and isolated contexts on conventional and free-threaded Python.](../assets/measurement-cpu.svg)

</div>

Each job runs a 3,000,000-step integer loop, about 0.33 s alone. Free-threaded workers used all 8 CPUs; conventional workers took turns, while isolated contexts overlapped on both builds.

## Fan out: four CPU jobs

![Execution spans for four CPU jobs across asyncio, threads, processes and PyBEAM modes, with measured wall time and speedup.](../assets/fan-out.svg)

With the GIL, asyncio, threads and `worker` ran the jobs one at a time. Free-threaded threads and workers overlapped them, as did isolated contexts, processes, prefork pools and separate nodes.

## Blast radius: one job aborts

<div class="measurement-chart" tabindex="0" role="region" aria-label="Scrollable measurement chart">

![Replacement time after one of four jobs aborts, labelled with the number of surviving jobs for each process boundary.](../assets/measurement-blast.svg)

</div>

An `os.abort()` in asyncio or threads kills the app; in `worker`, it kills the VM, so neither has a replacement timing. A new prefork pool does not recover its interrupted jobs.

## Fan in: many processes, eight contexts

<div class="measurement-chart" tabindex="0" role="region" aria-label="Scrollable measurement chart">

![Completion time for 10,000, 100,000 and one million BEAM processes calling eight worker or isolated contexts on a logarithmic axis.](../assets/measurement-fanin.svg)

</div>

Each process makes one empty call, with at most 32 calls in flight. A million waiting processes used about 2.7 GB (2.7 KB each); the worker ceiling of about 32,000–43,000 calls per second was not traced.

## Not measured

Web serving on conventional and free-threaded Python has its own [ASGI benchmarks](https://github.com/h0rv/pybeam/blob/main/benchmarks/web/SCALE_RESULTS.md).

- Actor throughput, memory and startup cost.

<style>
.measurement-chart { overflow-x: auto; }
.measurement-chart img { width: 100%; min-width: 680px; max-inline-size: none; height: auto; }
.measurement-chart:focus-visible { outline: 2px solid currentColor; outline-offset: 2px; }
</style>

## Appendix: environment and provenance

- Linux x86-64, 8 CPUs, 31 GiB memory; CPython 3.14.10 (GIL enabled) and 3.14.10t (free-threaded), Erlang/OTP 29.1.2.
- Context, CPU and fan-in charts used erlang-python `d60392c`: one conventional plus two free-threaded context-matrix runs. See [actor capacity reproduction](https://github.com/h0rv/pybeam/blob/main/docs/research/actor-capacity.md#recorded-method-and-reproduction).
- Four-job fan-out and blast-radius charts used erlang-python `c007eb5` probes, not controlled same-revision comparisons. See [execution-mode reproduction](https://github.com/h0rv/pybeam/blob/main/docs/research/execution-modes.md#four-job-fan-out-and-blast-radius-reproduction).
