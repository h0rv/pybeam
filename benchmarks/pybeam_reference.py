import shutil
import sys
import time
from pathlib import Path

from benchmarks.contract import AdapterResult, Config, Identity, Payload, StrictModel, Unsupported
from benchmarks.ownership import resolve, spawn_owned
from benchmarks.pybeam_reference_build import BuildManifest, verify


class Bootstrap(StrictModel):
    argv: tuple[str, ...]
    python_paths: tuple[str, ...]
    ready: str
    completed: str


class LaunchEvidence(StrictModel):
    guardian_owned_vm: Identity
    startup_ns: int
    shutdown_observation_ns: int
    vm_exited_before_cleanup_ack: bool
    launch_path: str = "guardian spawn_owned -> escript -> existing py_launch + runtime.loader"


def unsupported(config: Config) -> Unsupported | None:
    w = config.workload
    if (
        config.backend != "pybeam"
        or w.id != "rooms.local"
        or w.activity != "uniform"
        or w.payload != Payload()
        or config.unsafe_native_crash
    ):
        return Unsupported(
            reason="Legacy raw actor adapter supports only uniform rooms.local scalar 1",
            features=("requested workload, execution mode, or payload",),
        )
    if w.queue_bound_scope != "client_admission_only":
        return Unsupported(
            reason="Actual PyBEAM actors require explicit UnboundedMailbox",
            features=("finite destination queue", "raw transport input byte bound"),
        )
    return None


def run(directory: Path) -> AdapterResult | Unsupported:
    config = Config.model_validate_json((directory / "config.json").read_text())
    refusal = unsupported(config)
    if refusal is not None:
        return refusal
    root = Path(__file__).resolve().parent.parent
    if root != Path(config.python_source).resolve():
        raise ValueError("Adapter source does not match requested Python checkout")
    native = Path(config.native_source).resolve()
    build_path = native / "_build/pybeam-reference-build.json"
    manifest = BuildManifest.model_validate_json(build_path.read_text())
    verify(manifest, native)
    (directory / "native-build-manifest.json").write_text(manifest.model_dump_json(indent=2))
    runtime = Path(manifest.runtime_directory)
    escript = shutil.which("escript")
    if escript is None:
        raise RuntimeError("escript executable is unavailable")
    bootstrap = Bootstrap(
        argv=(str(root / "benchmarks/pybeam_reference_worker.py"), str(directory)),
        python_paths=(str(root), str(root / "src"), str(native / "priv"), *sys.path),
        ready=str(directory / "ready"),
        completed=str(directory / "completed"),
    )
    (directory / "bootstrap.json").write_text(bootstrap.model_dump_json())
    start = time.monotonic_ns()
    vm = spawn_owned(
        directory,
        (
            escript,
            str(root / "benchmarks/native_pybeam_bootstrap.escript"),
            str(root),
            str(runtime),
            str(directory),
        ),
    )
    deadline = time.monotonic() + config.budget.wall_seconds
    while not (directory / "ready").exists():
        if resolve(vm) is None:
            raise RuntimeError("Owned native VM exited before Python entrypoint readiness")
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned native VM startup timed out")
        time.sleep(0.01)
    startup = time.monotonic_ns() - start
    while resolve(vm) is not None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned native VM did not stop within the trial deadline")
        time.sleep(0.01)
    shutdown_observation = time.monotonic_ns()
    if (directory / "completed").read_text() != "0":
        raise RuntimeError("Embedded Python worker failed")
    if (directory / "cleanup").read_text() != "clean":
        raise RuntimeError("Native cooperative shutdown was not acknowledged")
    output = AdapterResult.model_validate_json((directory / "worker-result.json").read_text())
    verify(manifest, native)
    evidence = LaunchEvidence(
        guardian_owned_vm=vm,
        startup_ns=startup,
        shutdown_observation_ns=shutdown_observation,
        vm_exited_before_cleanup_ack=True,
    )
    (directory / "launch-evidence.json").write_text(evidence.model_dump_json(indent=2))
    return AdapterResult.model_validate_json(
        output.model_copy(update={"cleanup_acknowledged": True}).model_dump_json()
    )


def main() -> None:
    directory = Path(sys.argv[1]).resolve()
    output = run(directory)
    (directory / "adapter.json").write_text(output.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
