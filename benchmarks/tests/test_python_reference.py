import asyncio
import time
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from benchmarks import python_reference
from benchmarks.contract import (
    AdapterResult,
    Applied,
    Budget,
    Config,
    Payload,
    Unsupported,
    Workload,
    check_correctness,
    messages,
)
from benchmarks.python_reference import Peaks, Pending, dispatch, main, run


@pytest.fixture
def config() -> Config:
    root = Path(__file__).resolve().parents[2]
    return Config(
        python_source=str(root),
        native_source=str(root.parent / "native-benchmarks"),
        workload=Workload(
            entities=3, events=17, warmup_events=11, queue_capacity=1, max_inflight=4
        ),
        repeats=1,
    )


@pytest.mark.parametrize("scope", ["destination_queue", "client_admission_only"])
@pytest.mark.parametrize("window", [1, 4, 20])
def test_reference__matches_oracle_and_resets_same_workers(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    scope: Literal["destination_queue", "client_admission_only"],
    window: int,
) -> None:
    created: list[int] = []
    original = python_reference.worker

    async def tracked(entity: int, queue: asyncio.Queue[Pending | None], counts: list[int]) -> None:
        created.append(entity)
        await original(entity, queue, counts)

    monkeypatch.setattr(python_reference, "worker", tracked)
    config = config.model_copy(
        update={
            "workload": config.workload.model_copy(
                update={
                    "queue_bound_scope": scope,
                    "max_inflight": window,
                }
            )
        }
    )
    result = asyncio.run(run(config))
    assert isinstance(result, AdapterResult)
    check_correctness(config, result)
    assert sorted(created) == list(range(3))
    assert sum(result.terminal_counts) == 17
    assert result.entities_deleted == 3 and result.cleanup_acknowledged
    assert result.queue_peak == 1
    assert result.inflight_peak is not None and 0 < result.inflight_peak <= window
    assert result.transport_payload_bytes is None
    assert result.runtime.workers == result.runtime.interpreters == 1
    assert result.runtime.gil_enabled is True
    assert result.runtime.loaded_assets == () and result.runtime.native_source_sha is None


@pytest.mark.parametrize("damage", ["duplicate", "route", "state", "deletion", "queue", "span"])
def test_reference__oracle_rejects_damaged_evidence(config: Config, damage: str) -> None:
    result = asyncio.run(run(config))
    assert isinstance(result, AdapterResult)
    match damage:
        case "duplicate":
            result = result.model_copy(update={"receipts": (result.receipts[0],) * 17})
        case "route":
            result = result.model_copy(
                update={
                    "receipts": tuple(r.model_copy(update={"entity": 3}) for r in result.receipts)
                }
            )
        case "state":
            result = result.model_copy(update={"terminal_counts": (0, 0, 0)})
        case "deletion":
            result = result.model_copy(update={"entities_deleted": 0})
        case "queue":
            result = result.model_copy(update={"queue_peak": 2})
        case "span":
            result = result.model_copy(
                update={
                    "phases": result.phases.model_copy(update={"measurement_ns": 1}),
                }
            )
    with pytest.raises(ValueError):
        check_correctness(config, result)


@pytest.mark.parametrize(
    "workload",
    [
        Workload(id="rooms.churn"),
        Workload(id="rooms.isolation"),
        Workload(id="failure.reset"),
        Workload(id="backpressure.cancel"),
        Workload(id="bridge.individual"),
        Workload(id="bridge.batch"),
        Workload(id="execution.modes"),
        Workload(id="two_node.feasibility"),
        Workload(id="bounded.longrun"),
        Workload(activity="idle"),
        Workload(activity="hot_10_percent"),
        Workload(payload=Payload(kind="structured")),
        Workload(payload=Payload(kind="large_binary")),
        Workload(payload=Payload(encoding="other")),
        Workload(payload=Payload(encoded_bytes=2)),
        Workload(payload=Payload(batch_size=2)),
    ],
)
def test_reference__unsupported_has_no_measurements(config: Config, workload: Workload) -> None:
    output = asyncio.run(run(config.model_copy(update={"workload": workload})))
    assert isinstance(output, Unsupported)
    assert set(output.model_dump()) == {"kind", "reason", "features"}


def test_reference__rejects_free_threaded_mode(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(python_reference.sys, "_is_gil_enabled", lambda: False)
    output = asyncio.run(run(config))
    assert isinstance(output, Unsupported)


def test_reference__zero_warmup_is_valid(config: Config) -> None:
    config = config.model_copy(
        update={
            "workload": config.workload.model_copy(update={"warmup_events": 0}),
        }
    )
    output = asyncio.run(run(config))
    assert isinstance(output, AdapterResult)
    check_correctness(config, output)


def test_reference__rejects_native_backend_and_unsafe_crash(config: Config) -> None:
    for update in (
        {"backend": "erlang", "adapter_command": ("erl",)},
        {"unsafe_native_crash": True, "adapter_command": ("python",)},
    ):
        output = asyncio.run(run(config.model_copy(update=update)))
        assert isinstance(output, Unsupported)


@pytest.mark.parametrize("value", ['"17"', "true", "0", "17.0"])
def test_main__bad_config_produces_no_score(
    config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    source = config.model_dump_json().replace('"events":17', '"events":' + value)
    (tmp_path / "config.json").write_text(source)
    monkeypatch.setattr(python_reference.sys, "argv", ["reference", str(tmp_path)])
    with pytest.raises(ValidationError):
        main()
    assert not (tmp_path / "adapter.json").exists()


def test_reference__worker_failure_cancels_every_task(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(entity: int, queue: asyncio.Queue[Pending | None], counts: list[int]) -> None:
        await queue.get()
        raise RuntimeError("injected worker failure after delivery")

    monkeypatch.setattr(python_reference, "worker", failing)

    async def scenario() -> None:
        before = asyncio.all_tasks()
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            await run(config)
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("deadline", [False, True])
def test_reference__cancellation_and_timeout_join_workers(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    deadline: bool,
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        closed: list[int] = []
        never = asyncio.Event()

        async def blocked(
            entity: int, queue: asyncio.Queue[Pending | None], counts: list[int]
        ) -> None:
            entered.set()
            try:
                await never.wait()
            finally:
                closed.append(entity)

        monkeypatch.setattr(python_reference, "worker", blocked)
        before = asyncio.all_tasks()
        if deadline:
            request = config.model_copy(update={"budget": Budget(wall_seconds=0.01)})
            with pytest.raises(TimeoutError):
                await run(request)
        else:
            async with asyncio.TaskGroup() as group:
                task = group.create_task(run(config))
                await entered.wait()
                task.cancel()
            assert task.cancelled()
        assert sorted(closed) == list(range(3))
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_dispatch__sender_latency_includes_window_admission() -> None:
    async def scenario() -> None:
        queue: asyncio.Queue[Pending | None] = asyncio.Queue(maxsize=1)
        entered = asyncio.Event()
        release = asyncio.Event()
        peaks = Peaks()

        async def receiver() -> None:
            for _ in range(2):
                pending = await queue.get()
                assert pending is not None
                entered.set()
                await release.wait()
                pending.applied.set_result(Applied(pending.message.id, 0, 1))
                queue.task_done()

        async with asyncio.TaskGroup() as group:
            group.create_task(receiver())
            task = group.create_task(dispatch(messages(Workload(entities=1), 2), [queue], 1, peaks))
            await entered.wait()
            released_ns = time.monotonic_ns()
            release.set()
        receipts = sorted(task.result(), key=lambda r: r.id)
        assert receipts[1].sent_ns < released_ns <= receipts[1].completed_ns
        assert peaks.inflight == 0 and peaks.inflight_peak == 1

    asyncio.run(scenario())
