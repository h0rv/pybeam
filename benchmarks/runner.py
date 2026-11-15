import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Literal

import psutil
from pydantic import TypeAdapter

from benchmarks.contract import (
    AdapterOutput,
    AdapterResult,
    Config,
    Outcome,
    Phases,
    ResourceSample,
    Statistics,
    StrictModel,
    check_correctness,
)
from benchmarks.metrics import describe
from benchmarks.ownership import Control, identity, resolve
from benchmarks.provenance import capture

type Quantile = Literal["p50", "p95", "p99"]
type Phase = Literal["creation_ns", "warmup_ns", "measurement_ns", "deletion_ns"]


class TrialSummary(StrictModel):
    trial: int
    outcome: Outcome
    completed: int | None
    throughput_per_second: float | None
    latency_ns: Statistics | None
    phases: Phases | None
    adapter_rss_peak_bytes: int | None
    descendants_rss_peak_bytes: int | None
    simultaneous_tree_rss_peak_bytes: int | None
    cpu_seconds_observed_by_pid: dict[str, float]


class Summary(StrictModel):
    host_control: str
    trials: tuple[TrialSummary, ...]
    throughput_repeat_statistics: Statistics | None
    latency_quantile_repeat_statistics: dict[Quantile, Statistics]
    phase_repeat_statistics: dict[Phase, Statistics]


@contextlib.contextmanager
def measurement_lock() -> Generator[int]:
    # Shared path is deliberate; O_NOFOLLOW prevents following an attacker symlink.
    path = Path("/tmp") / ("pybeam-benchmark-load-" + str(os.getuid()) + ".lock")  # noqa: S108
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another benchmark owns the shared load lock") from error
        yield descriptor
    finally:
        os.close(descriptor)


def trial(config: Config, directory: Path, lock_fd: int, control: Control | None = None) -> Outcome:
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(config.model_dump_json(indent=2) + "\n")
    parent = identity(psutil.Process())
    with (directory / "guardian.log").open("wb") as log:
        guardian = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "benchmarks.guardian",
                str(directory),
                parent.model_dump_json(),
                *([] if control is None else [control.model_dump_json()]),
            ],
            start_new_session=True,
            pass_fds=(lock_fd,),
            stdout=log,
            stderr=log,
        )
        try:
            guardian.wait(timeout=config.budget.wall_seconds + 8)
        except subprocess.TimeoutExpired as error:
            # Keep the guardian alive to own cleanup. The load lock remains held there.
            raise RuntimeError(
                "Guardian did not finish; inspect guardian.log before further load"
            ) from error
    path = directory / "outcome.json"
    if guardian.returncode != 0 or not path.exists():
        raise RuntimeError("Guardian failed; inspect " + str(directory / "guardian.log"))
    return Outcome.model_validate_json(path.read_text())


def summarize(index: int, directory: Path, config: Config, outcome: Outcome) -> TrialSummary:
    samples = tuple(
        ResourceSample.model_validate_json(line)
        for line in (directory / "resources.jsonl").read_text().splitlines()
    )
    completed: int | None = None
    throughput: float | None = None
    latency: Statistics | None = None
    phases: Phases | None = None
    if outcome.status == "passed":
        output = TypeAdapter(AdapterOutput).validate_json((directory / "adapter.json").read_text())
        if not isinstance(output, AdapterResult):
            raise ValueError("Passing trial must contain completion evidence")
        check_correctness(config, output)
        phases = output.phases
        completed = len(output.receipts)
        throughput = completed * 1e9 / output.phases.measurement_ns
        latency = describe(tuple(float(r.completed_ns - r.sent_ns) for r in output.receipts))
    cpu: dict[str, float] = {}
    for sample in samples:
        for process in sample.processes:
            key = str(process.identity.pid) + "@" + str(process.identity.created)
            cpu[key] = max(cpu.get(key, 0.0), process.cpu_seconds)
    return TrialSummary(
        trial=index,
        outcome=outcome,
        completed=completed,
        throughput_per_second=throughput,
        latency_ns=latency,
        phases=phases,
        adapter_rss_peak_bytes=max(
            (
                sum(p.rss_bytes for p in sample.processes if p.role == "adapter")
                for sample in samples
            ),
            default=None,
        ),
        descendants_rss_peak_bytes=max(
            (
                sum(p.rss_bytes for p in sample.processes if p.role == "descendant")
                for sample in samples
            ),
            default=None,
        ),
        simultaneous_tree_rss_peak_bytes=max(
            (sum(p.rss_bytes for p in sample.processes) for sample in samples), default=None
        ),
        cpu_seconds_observed_by_pid=cpu,
    )


def run(config: Config, output: Path, control: Control | None = None) -> Summary:
    with measurement_lock() as descriptor:
        output.mkdir(parents=True, exist_ok=False)
        provenance = capture(config.python_source, config.native_source)
        (output / "provenance.json").write_text(provenance.model_dump_json(indent=2) + "\n")
        summaries: list[TrialSummary] = []
        with (output / "trials.jsonl").open("w") as raw:
            for index in range(config.repeats):
                if control is not None and (
                    Path(control.stop_file).exists() or resolve(control.owner) is None
                ):
                    break
                directory = output / ("trial-" + str(index))
                outcome = trial(config, directory, descriptor, control)
                summary = summarize(index, directory, config, outcome)
                summaries.append(summary)
                raw.write(summary.model_dump_json() + "\n")
                raw.flush()
                print(
                    "Trial {}: {} ({} completed)".format(index, outcome.status, summary.completed)
                )
                if summary.latency_ns is not None:
                    latency = summary.latency_ns
                    print(
                        "  Diagnostic latency ns p50={} p95={} p99={}; "
                        "observed RSS adapter={} descendants={} tree={}".format(
                            latency.p50,
                            latency.p95,
                            latency.p99,
                            summary.adapter_rss_peak_bytes,
                            summary.descendants_rss_peak_bytes,
                            summary.simultaneous_tree_rss_peak_bytes,
                        )
                    )
                if outcome.status != "passed":
                    break
        rates = tuple(
            s.throughput_per_second for s in summaries if s.throughput_per_second is not None
        )
        latency_values: dict[Quantile, list[float]] = {"p50": [], "p95": [], "p99": []}
        phase_values: dict[Phase, list[float]] = {
            "creation_ns": [],
            "warmup_ns": [],
            "measurement_ns": [],
            "deletion_ns": [],
        }
        for summary in summaries:
            if summary.latency_ns is not None:
                latency_values["p50"].append(summary.latency_ns.p50)
                latency_values["p95"].append(summary.latency_ns.p95)
                latency_values["p99"].append(summary.latency_ns.p99)
            if summary.phases is not None:
                phase_values["creation_ns"].append(float(summary.phases.creation_ns))
                phase_values["warmup_ns"].append(float(summary.phases.warmup_ns))
                phase_values["measurement_ns"].append(float(summary.phases.measurement_ns))
                phase_values["deletion_ns"].append(float(summary.phases.deletion_ns))
        result = Summary(
            host_control=config.host_control,
            trials=tuple(summaries),
            throughput_repeat_statistics=describe(rates) if rates else None,
            latency_quantile_repeat_statistics={
                key: describe(tuple(values)) for key, values in latency_values.items() if values
            },
            phase_repeat_statistics={
                key: describe(tuple(values)) for key, values in phase_values.items() if values
            },
        )
        (output / "summary.json").write_text(result.model_dump_json(indent=2) + "\n")
        if result.throughput_repeat_statistics is not None:
            stats = result.throughput_repeat_statistics
            print("Diagnostic completed/s mean={} sample stdev={}".format(stats.mean, stats.stdev))
        print(
            "Uncontrolled smoke only. No comparative performance claim. Artifacts: " + str(output)
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared benchmark correctness harness")
    parser.add_argument("command", choices=("run", "schema"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--control", help="Local owner identity and stop-file JSON, not HTTP input")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.command == "schema":
        schemas = root / "benchmarks" / "schemas"
        schemas.mkdir(exist_ok=True)
        for name, schema in (
            ("config", Config.model_json_schema()),
            ("adapter", TypeAdapter(AdapterOutput).json_schema()),
            ("outcome", Outcome.model_json_schema()),
            ("summary", Summary.model_json_schema()),
        ):
            (schemas / (name + ".schema.json")).write_text(json.dumps(schema, indent=2) + "\n")
        return
    config = (
        Config.model_validate_json(args.config.read_text())
        if args.config is not None
        else Config(python_source=str(root), native_source=str(root.parent / "native-benchmarks"))
    )
    output = args.output or root / "benchmarks" / "results" / (
        time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    )
    output = output.resolve()
    if not output.is_relative_to(root / "benchmarks" / "results") and not output.is_relative_to(
        Path("/tmp").resolve()  # noqa: S108  User-authorized artifact location.
    ):
        parser.error("Artifacts must be under this experiment's benchmarks/results or /tmp")
    control = None if args.control is None else Control.model_validate_json(args.control)
    result = run(config, output, control)
    if any(t.outcome.status == "failed" for t in result.trials):
        raise SystemExit(1)
