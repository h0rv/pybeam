from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pybeam.runtime.ownership import Identity
from pydantic import BaseModel, ConfigDict, Field, model_validator

type Positive = Annotated[int, Field(gt=0)]
type Natural = Annotated[int, Field(ge=0)]
type Duration = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type Family = Literal[
    "rooms.local",
    "rooms.churn",
    "rooms.isolation",
    "failure.reset",
    "backpressure.cancel",
    "bridge.individual",
    "bridge.batch",
    "execution.modes",
    "two_node.feasibility",
    "bounded.longrun",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Budget(StrictModel):
    wall_seconds: Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)] = 15.0
    tree_rss_bytes: Positive = 536_870_912
    processes: Annotated[int, Field(ge=1, le=32)] = 8
    threads: Annotated[int, Field(ge=1, le=128)] = 32
    artifact_bytes: Positive = 16_777_216
    sample_seconds: Annotated[float, Field(ge=0.01, le=1, allow_inf_nan=False)] = 0.02
    cpu_cores_requested: Literal[1] = 1


class Payload(StrictModel):
    kind: Literal["scalar", "structured", "large_binary"] = "scalar"
    encoding: str = "json-utf8"
    encoded_bytes: Positive = 1
    batch_size: Positive = 1


class Workload(StrictModel):
    id: Family = "rooms.local"
    entities: Annotated[int, Field(ge=1, le=10_000)] = 16
    events: Annotated[int, Field(ge=1, le=100_000)] = 1000
    warmup_events: Annotated[int, Field(ge=0, le=1000)] = 100
    seed: Natural = 1
    activity: Literal["uniform", "hot_10_percent", "idle"] = "uniform"
    queue_capacity: Annotated[int, Field(ge=1, le=1024)] = 16
    queue_bound_scope: Literal["destination_queue", "client_admission_only"] = "destination_queue"
    max_inflight: Annotated[int, Field(ge=1, le=4096)] = 16
    ack: Literal["applied"] = "applied"
    delivery: Literal["exactly_once_no_retry"] = "exactly_once_no_retry"
    payload: Payload = Payload()


class Config(StrictModel):
    schema_version: Literal[1] = 1
    backend: Literal["python", "pybeam", "erlang", "elixir"] = "python"
    adapter_command: tuple[str, ...] = ()
    workload: Workload = Workload()
    budget: Budget = Budget()
    repeats: Annotated[int, Field(ge=1, le=10)] = 3
    scale: Literal["smoke", "explicit_progressive"] = "smoke"
    unsafe_native_crash: bool = False
    host_control: Literal["uncontrolled_smoke"] = "uncontrolled_smoke"
    python_source: str
    native_source: str | None = None

    @model_validator(mode="after")
    def safe_smoke(self) -> Self:
        w = self.workload
        if self.backend == "pybeam" and self.native_source is None:
            raise ValueError("PyBEAM requires an explicitly configured native source checkout")
        if self.scale == "smoke" and (w.entities > 20 or w.events > 1000):
            raise ValueError("Larger workloads require explicit_progressive")
        if self.backend != "python" and not self.adapter_command:
            raise ValueError("A non-Python backend requires an explicit adapter command")
        if self.unsafe_native_crash and not self.adapter_command:
            raise ValueError("Unsafe crash probes require an explicit owned adapter")
        return self


class Message(StrictModel):
    id: Natural
    entity: Natural
    increment: Literal[1] = 1


def messages(workload: Workload, count: int) -> tuple[Message, ...]:
    """Canonical scalar sequence. IDs restart at zero after warmup reset."""
    return tuple(
        Message(id=i, entity=(i + workload.seed) % workload.entities) for i in range(count)
    )


@dataclass(frozen=True, slots=True)
class Applied:
    """Handler reply after applying the effect, not a producer's request label."""

    id: int
    entity: int
    count: int


def verify_applied(message: Message, reply: Applied) -> None:
    if (reply.id, reply.entity) != (message.id, message.entity):
        raise ValueError("Applied acknowledgment has wrong message or executing entity identity")


class Receipt(StrictModel):
    id: Natural
    entity: Natural
    sent_ns: Natural
    completed_ns: Natural

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.completed_ns < self.sent_ns:
            raise ValueError("Receipt timestamps must share the sender's monotonic clock")
        return self


class LoadedAsset(StrictModel):
    path: str
    sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class Runtime(StrictModel):
    python: str | None
    otp: str | None
    erts: str | None
    elixir: str | None
    mode: str
    gil_enabled: bool | None
    interpreters: Positive | None
    workers: Positive | None
    schedulers: Positive | None
    executable: str
    executable_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    native_source_sha: Annotated[str, Field(pattern="^[0-9a-f]{40}$")] | None
    loaded_assets: tuple[LoadedAsset, ...] = ()


class Phases(StrictModel):
    creation_ns: Natural
    warmup_ns: Natural
    measurement_ns: Positive
    deletion_ns: Natural


class AdapterResult(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["result"] = "result"
    backend: Literal["python", "pybeam", "erlang", "elixir"]
    workload: Workload
    runtime: Runtime
    clock: Literal["sender_monotonic_ns"] = "sender_monotonic_ns"
    phases: Phases
    receipts: tuple[Receipt, ...]
    transport_encoding: str
    transport_payload_bytes: Natural | None
    terminal_counts: tuple[Natural, ...]
    entities_created: Natural
    entities_deleted: Natural
    queue_peak: Natural | None
    inflight_peak: Natural | None
    failures: Natural
    recovered: Natural | None
    lost: Natural
    cleanup_acknowledged: bool
    semantic_differences: tuple[str, ...] = ()
    unsupported_features: tuple[str, ...] = ()


class Unsupported(StrictModel):
    kind: Literal["unsupported"] = "unsupported"
    reason: str
    features: tuple[str, ...]


type AdapterOutput = Annotated[AdapterResult | Unsupported, Field(discriminator="kind")]


def check_correctness(config: Config, result: AdapterResult) -> None:
    """Reject invalid completion evidence before calculating any performance metric."""
    w = config.workload
    if w.id != "rooms.local" or w.activity != "uniform" or w.payload != Payload():
        raise ValueError("This contract version has no correctness oracle for that workload")
    if result.backend != config.backend or result.workload != w:
        raise ValueError("Backend or workload differs from request")
    runtime = result.runtime
    if result.backend in {"python", "pybeam"} and (
        runtime.python is None or runtime.gil_enabled is None or runtime.interpreters is None
    ):
        raise ValueError("Python runtime metadata is incomplete")
    if result.backend != "python" and (
        runtime.otp is None
        or runtime.erts is None
        or runtime.schedulers is None
        or not runtime.loaded_assets
    ):
        raise ValueError("Native runtime metadata is incomplete")
    if result.backend == "python" and (
        runtime.otp is not None
        or runtime.erts is not None
        or runtime.native_source_sha is not None
        or runtime.loaded_assets
    ):
        raise ValueError("Python-only backend cannot claim native runtime assets")
    asset_paths = tuple(asset.path for asset in runtime.loaded_assets)
    nif_assets = tuple(path for path in asset_paths if path.endswith(("py_nif.so", "py_nif.dylib")))
    if result.backend == "pybeam" and (
        runtime.native_source_sha is None
        or not nif_assets
        or not any(path.endswith(".beam") for path in asset_paths)
    ):
        raise ValueError("PyBEAM requires paired native revision, NIF and BEAM assets")
    if result.backend in {"erlang", "elixir"}:
        if runtime.python is not None or nif_assets or runtime.native_source_sha is not None:
            raise ValueError("Pure OTP backend cannot claim Python or paired PyBEAM NIF identity")
        if not any(path.endswith(".beam") for path in asset_paths) or not any(
            path.endswith((".erl", ".ex", ".exs")) for path in asset_paths
        ):
            raise ValueError("Pure OTP requires adapter BEAM and source hashes")
        if runtime.executable.rsplit("/", 1)[-1] not in {"beam.smp", "beam"}:
            raise ValueError("Pure OTP requires the actual ERTS executable")
    if result.backend == "elixir" and runtime.elixir is None:
        raise ValueError("Elixir runtime metadata is incomplete")
    if result.backend == "pybeam" and w.queue_bound_scope == "destination_queue":
        raise ValueError("Finite native actor admission is unsupported; do not claim a bound")
    expected = messages(w, w.events)
    actual = {(r.id, r.entity) for r in result.receipts}
    if len(result.receipts) != w.events or actual != {(m.id, m.entity) for m in expected}:
        raise ValueError("Missing, duplicate, or misrouted completions")
    counts = Counter(m.entity for m in expected)
    if result.terminal_counts != tuple(counts[i] for i in range(w.entities)):
        raise ValueError("Terminal entity state differs from applied messages")
    if result.entities_created != w.entities or result.entities_deleted != w.entities:
        raise ValueError("Entity lifecycle count differs from request")
    if result.failures or result.lost or not result.cleanup_acknowledged:
        raise ValueError("Unexpected failure, loss, or missing cleanup acknowledgment")
    if result.inflight_peak is None:
        raise ValueError("Missing observed client admission bound")
    if w.queue_bound_scope == "destination_queue" and result.queue_peak is None:
        raise ValueError("Missing observed destination queue bound")
    if result.inflight_peak > w.max_inflight:
        raise ValueError("Observed inflight work exceeds admission budget")
    if result.queue_peak is not None and result.queue_peak > w.queue_capacity:
        raise ValueError("Observed destination queue exceeds requested capacity")
    if result.receipts:
        span = max(r.completed_ns for r in result.receipts) - min(
            r.sent_ns for r in result.receipts
        )
        if span > result.phases.measurement_ns:
            raise ValueError("Completion evidence falls outside measurement duration")


class ProcessSample(StrictModel):
    identity: Identity
    role: Literal["adapter", "descendant"]
    rss_bytes: Natural
    cpu_seconds: Duration
    threads: Natural


class ResourceSample(StrictModel):
    elapsed_ns: Natural
    harness_rss_bytes: Natural
    guardian_rss_bytes: Natural
    harness_cpu_seconds: Duration
    guardian_cpu_seconds: Duration
    processes: tuple[ProcessSample, ...]


class Source(StrictModel):
    path: str
    sha: str
    dirty: bool
    diff_sha256: str
    working_tree_sha256: str


class Outcome(StrictModel):
    status: Literal["passed", "failed", "unsupported"]
    reason: str | None
    returncode: int | None
    cleanup_survivors: tuple[Identity, ...]
    cleanup_state: Literal["confirmed", "uncertain"] = "confirmed"
    cleanup_ns: Natural
    forced_cleanup_count: Natural
    enforcement: tuple[str, ...]


class Statistics(StrictModel):
    count: Positive
    mean: Duration
    stdev: Duration
    minimum: Duration
    maximum: Duration
    p50: Duration
    p95: Duration
    p99: Duration
