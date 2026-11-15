import contextlib
import hashlib
import math
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

import psutil
from pybeam.runtime.guardian import AuthorizationError, GuardianEndpoint, acknowledge_registered
from pydantic import TypeAdapter

from benchmarks.contract import (
    AdapterOutput,
    Config,
    Identity,
    Outcome,
    ProcessSample,
    ResourceSample,
    Runtime,
    Unsupported,
    check_correctness,
)
from benchmarks.ownership import (
    Control,
    Registry,
    SpawnReply,
    SpawnRequest,
    cleanup,
    discover,
    durable_write,
    identity,
    record,
    resolve,
    write_lease,
)
from benchmarks.provenance import git


def snapshot(value: Identity, role: str) -> ProcessSample | None:
    process = resolve(value)
    if process is None:
        return None
    try:
        cpu = process.cpu_times()
        return ProcessSample(
            identity=value,
            role="adapter" if role == "adapter" else "descendant",
            rss_bytes=process.memory_info().rss,
            cpu_seconds=cpu.user + cpu.system,
            threads=process.num_threads(),
        )
    except psutil.NoSuchProcess:
        return None


def artifact_size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def append_sample(stream: TextIO, sample: ResourceSample) -> None:
    stream.write(sample.model_dump_json() + "\n")
    stream.flush()


def verify_runtime_assets(config: Config, runtime: Runtime) -> None:
    """Check adapter attestations against checkout identities and actual file bytes.

    These checks are not independent proof of a VM's memory mappings.
    """
    if runtime.native_source_sha is not None and (
        config.native_source is None
        or runtime.native_source_sha
        != git(Path(config.native_source), "rev-parse", "HEAD").decode().strip()
    ):
        raise ValueError("Adapter loaded a different native source revision")
    asset_root = Path(config.python_source).resolve()
    if config.backend == "pybeam":
        if config.native_source is None:
            raise ValueError("PyBEAM native source checkout is required")
        asset_root = Path(config.native_source).resolve()
    if config.backend != "python" and any(
        not Path(asset.path).resolve().is_relative_to(asset_root) for asset in runtime.loaded_assets
    ):
        raise ValueError("Adapter asset is outside its declared source checkout")
    for path, digest in (
        (runtime.executable, runtime.executable_sha256),
        *((asset.path, asset.sha256) for asset in runtime.loaded_assets),
    ):
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError("Runtime executable or loaded asset hash differs")


def supervise(directory: Path, parent: Identity, control: Control | None = None) -> Outcome:
    config = Config.model_validate_json((directory / "config.json").read_text())
    budget = config.budget
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cpu_limit = math.ceil(budget.wall_seconds) + 2
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    environment.update(
        PYTHONHASHSEED=str(config.workload.seed % 4_294_967_296),
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        ERL_FLAGS="+S 1:1 +A 1",
        PYTHONUNBUFFERED="1",
    )
    command = config.adapter_command or (sys.executable, "-m", "benchmarks.python_smoke")
    owned: set[Identity] = set()
    children: list[subprocess.Popen[bytes]] = []
    enforcement = [
        "Independent guardian checks harness PID and creation time.",
        "RSS/process/thread/artifact budgets are sampled watchdog limits, not hard allocations.",
        "RSS is summed resident memory, not PSS; shared pages may be counted more than once.",
        "RLIMIT_CORE=0 and per-process RLIMIT_CPU inherited; no RLIMIT_RSS/AS guarantee.",
        "Detached runtimes must use guardian spawn broker; unregistered escape is unsupported.",
    ]
    reason: str | None = None
    status: str = "failed"
    start = time.monotonic_ns()
    endpoint = GuardianEndpoint(
        directory=directory.resolve(),
        owner=parent,
        guardian=identity(psutil.Process()),
        stop_file=None if control is None else Path(control.stop_file),
        control_owner=None if control is None else control.owner,
    )
    durable_write(directory / "guardian.json", endpoint.model_dump_json())
    with (
        (directory / "stdout.log").open("wb") as stdout,
        (directory / "stderr.log").open("wb") as stderr,
        (directory / "resources.jsonl").open("w") as samples,
        contextlib.ExitStack() as stack,
    ):
        record(directory, owned, "spawning")
        process = subprocess.Popen(  # noqa: S603
            [*command, str(directory)],
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        children.append(process)
        adapter = identity(psutil.Process(process.pid))
        owned.add(adapter)
        record(directory, owned, "tracking")
        try:
            if sys.platform == "linux":
                target = psutil.Process(process.pid)
                cpus = target.cpu_affinity()
                target.cpu_affinity(cpus[:1])
                enforcement.append("Linux adapter affinity pinned to one allowed CPU; inherited.")
            else:
                enforcement.append(
                    "CPU affinity unavailable here; one core is requested, not enforced."
                )
            while True:
                previous = frozenset(owned)
                discover(owned)
                if owned != previous:
                    record(directory, owned, "tracking")
                request_path = directory / "spawn.request.json"
                if request_path.exists():
                    request = SpawnRequest.model_validate_json(request_path.read_text())
                    index = len(children)
                    child_out = stack.enter_context(
                        (directory / (str(index) + ".stdout.log")).open("wb")
                    )
                    child_err = stack.enter_context(
                        (directory / (str(index) + ".stderr.log")).open("wb")
                    )
                    record(directory, owned, "spawning")
                    child = subprocess.Popen(  # noqa: S603
                        request.argv,
                        env=environment,
                        stdout=child_out,
                        stderr=child_err,
                        start_new_session=True,
                    )
                    children.append(child)
                    child_identity = identity(psutil.Process(child.pid))
                    owned.add(child_identity)
                    record(directory, owned, "tracking")
                    if sys.platform == "linux":
                        target = psutil.Process(child.pid)
                        target.cpu_affinity(cpus[:1])
                    reply = directory / "spawn.reply.pending"
                    reply.write_text(SpawnReply(identity=child_identity).model_dump_json())
                    os.replace(reply, directory / "spawn.reply.json")
                    request_path.unlink()
                observed = tuple(
                    sample
                    for value in owned
                    if (sample := snapshot(value, "adapter" if value == adapter else "descendant"))
                    is not None
                )
                harness = resolve(parent)
                guardian_cpu = psutil.Process().cpu_times()
                harness_cpu = None if harness is None else harness.cpu_times()
                append_sample(
                    samples,
                    ResourceSample(
                        elapsed_ns=time.monotonic_ns() - start,
                        harness_rss_bytes=0 if harness is None else harness.memory_info().rss,
                        guardian_rss_bytes=psutil.Process().memory_info().rss,
                        guardian_cpu_seconds=guardian_cpu.user + guardian_cpu.system,
                        harness_cpu_seconds=(
                            0.0 if harness_cpu is None else harness_cpu.user + harness_cpu.system
                        ),
                        processes=observed,
                    ),
                )
                if control is not None and Path(control.stop_file).exists():
                    reason = "Stop requested; side effects are not rolled back"
                elif control is not None and resolve(control.owner) is None:
                    reason = "Control owner exited"
                elif harness is None:
                    reason = "Harness owner exited"
                elif time.monotonic_ns() - start > budget.wall_seconds * 1e9:
                    reason = "Wall-time budget exceeded"
                elif sum(p.rss_bytes for p in observed) > budget.tree_rss_bytes:
                    reason = "Owned process-tree RSS budget exceeded"
                elif len(observed) > budget.processes:
                    reason = "Owned process count budget exceeded"
                elif sum(p.threads for p in observed) > budget.threads:
                    reason = "Owned thread count budget exceeded"
                elif artifact_size(directory) > budget.artifact_bytes:
                    reason = "Artifact byte budget exceeded"
                if reason is not None:
                    break
                for registration in directory.glob("register-*.json"):
                    acknowledge_registered(registration, owned, endpoint)
                if process.poll() is not None:
                    if process.returncode != 0:
                        reason = "Adapter exited unsuccessfully"
                        break
                    output = TypeAdapter(AdapterOutput).validate_json(
                        (directory / "adapter.json").read_text()
                    )
                    if isinstance(output, Unsupported):
                        status, reason = "unsupported", output.reason
                    else:
                        check_correctness(config, output)
                        verify_runtime_assets(config, output.runtime)
                        with (directory / "receipts.jsonl").open("w") as raw:
                            for receipt in output.receipts:
                                raw.write(receipt.model_dump_json() + "\n")
                        if artifact_size(directory) > budget.artifact_bytes:
                            raise ValueError("Evidence serialization exceeds artifact budget")
                        if any(resolve(value) is not None for value in owned):
                            raise ValueError(
                                "Adapter acknowledged cleanup with live owned processes"
                            )
                        status = "passed"
                    break
                time.sleep(budget.sample_seconds)
        except (AuthorizationError, OSError, ValueError, psutil.Error) as error:
            reason = type(error).__name__ + ": invalid adapter evidence or process observation"
        finally:
            durable_write(directory / "registration.closed", "closed")
            cleanup_start = time.monotonic_ns()
            forced = sum(resolve(value) is not None for value in owned)
            survivors = cleanup(owned)
            registry = Registry.model_validate_json((directory / "ownership.json").read_text())
            record(directory, owned, registry.phase)
            cleanup_duration = time.monotonic_ns() - cleanup_start
            for child in children:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=1.0)
    registry = Registry.model_validate_json((directory / "ownership.json").read_text())
    uncertain = bool(survivors) or registry.phase == "spawning"
    if uncertain:
        status, reason = "failed", "Ownership uncertain; pending spawn or surviving owned process"
    return Outcome(
        status="passed"
        if status == "passed"
        else "unsupported"
        if status == "unsupported"
        else "failed",
        reason=reason,
        returncode=process.returncode,
        cleanup_survivors=survivors,
        cleanup_state="uncertain" if uncertain else "confirmed",
        cleanup_ns=cleanup_duration,
        forced_cleanup_count=forced,
        enforcement=tuple(enforcement),
    )


def main() -> None:
    directory = Path(sys.argv[1])
    parent = Identity.model_validate_json(sys.argv[2])
    control = (
        Control.model_validate_json(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else None
    )
    outcome = supervise(directory, parent, control)
    registry = Registry.model_validate_json((directory / "ownership.json").read_text())
    record(directory, set(registry.owned), outcome.cleanup_state)
    if outcome.cleanup_state == "confirmed" and len(sys.argv) > 4:
        write_lease(int(sys.argv[4]), None)
    durable_write(directory / "outcome.json", outcome.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
