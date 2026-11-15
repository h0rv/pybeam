import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from benchmarks.contract import AdapterResult, Config, Receipt, Workload, check_correctness
from benchmarks.metrics import describe, percentile
from benchmarks.python_smoke import run


@pytest.fixture
def config() -> Config:
    root = Path(__file__).resolve().parents[2]
    return Config(python_source=str(root), native_source=str(root.parent / "native-benchmarks"))


def test_config__rejects_coercion_unknown_fields_and_accidental_scale(config: Config) -> None:
    data = json.loads(config.model_dump_json())
    data["workload"]["events"] = "1000"
    with pytest.raises(ValidationError):
        Config.model_validate_json(json.dumps(data))
    data["workload"]["events"] = 1001
    with pytest.raises(ValidationError, match="explicit_progressive"):
        Config.model_validate_json(json.dumps(data))
    data["workload"]["events"] = 1000
    data["unsafe"] = True
    with pytest.raises(ValidationError):
        Config.model_validate_json(json.dumps(data))


def test_smoke__completes_and_resets_warmup(config: Config) -> None:
    result = asyncio.run(run(config))
    assert isinstance(result, AdapterResult)
    check_correctness(config, result)
    assert sum(result.terminal_counts) == config.workload.events
    assert result.inflight_peak is not None and 0 < result.inflight_peak <= 16
    assert result.queue_peak is not None and result.queue_peak > 0
    assert len({r.id for r in result.receipts}) == 1000


@pytest.mark.parametrize("damage", ["duplicate", "state", "cleanup", "loss", "inflight", "span"])
def test_correctness__rejects_invalid_evidence(config: Config, damage: str) -> None:
    result = asyncio.run(run(config))
    assert isinstance(result, AdapterResult)
    match damage:
        case "duplicate":
            result = result.model_copy(update={"receipts": (result.receipts[0],) * 1000})
        case "state":
            result = result.model_copy(update={"terminal_counts": (0,) * 16})
        case "cleanup":
            result = result.model_copy(update={"cleanup_acknowledged": False})
        case "loss":
            result = result.model_copy(update={"lost": 1})
        case "inflight":
            result = result.model_copy(update={"inflight_peak": 17})
        case "span":
            result = result.model_copy(
                update={
                    "phases": result.phases.model_copy(update={"measurement_ns": 1}),
                }
            )
    with pytest.raises(ValueError):
        check_correctness(config, result)


def test_receipt__rejects_cross_origin_negative_duration() -> None:
    with pytest.raises(ValidationError):
        Receipt(id=0, entity=0, sent_ns=20, completed_ns=10)


def test_percentiles__use_nearest_rank_and_sample_variability() -> None:
    values = tuple(float(i) for i in range(1, 101))
    assert percentile(values, 0.50) == 50
    assert percentile(values, 0.95) == 95
    assert percentile(values, 0.99) == 99
    stats = describe((1.0, 2.0, 3.0))
    assert stats.mean == 2 and stats.stdev == 1
    assert describe((2.0,)).stdev == 0
    with pytest.raises(ValueError):
        describe(())


def test_non_smoke_workload__is_unsupported_not_zero(config: Config) -> None:
    unsupported = asyncio.run(
        run(config.model_copy(update={"workload": Workload(id="failure.reset")}))
    )
    assert unsupported.kind == "unsupported"
