import sys
from pathlib import Path

import psutil
import pytest
from pydantic import ValidationError

from benchmarks import guardian
from benchmarks.contract import Budget, Config, ProcessSample
from examples.distributed.config import configuration
from examples.distributed.policy import (
    ADMITTED_TASKS,
    MAIN_CONTEXTS,
    MAX_VMS,
    PROCESS_LIMIT,
    THREAD_LIMIT,
    VM_THREADS,
    walkthrough_budget,
)
from examples.showcase.catalog import ROOT
from pybeam.runtime.ownership import Identity, identity


def test_walkthrough_policy__accounts_for_two_fixed_vm_thread_sets() -> None:
    _, distribution = configuration()
    assert len(distribution.nodes) == MAX_VMS == 2
    assert distribution.capacity == ADMITTED_TASKS == 2
    assert MAIN_CONTEXTS == 1
    assert VM_THREADS == 18 + 1 + 2
    budget = walkthrough_budget()
    assert budget.processes == PROCESS_LIMIT == 16
    assert budget.threads == THREAD_LIMIT == 2 * 21 + 14 == 56
    assert Budget().threads == 32
    assert budget.wall_seconds == 120.0
    with pytest.raises(ValidationError):
        Budget(threads=129)
    with pytest.raises(ValidationError):
        Budget(processes=33)


def test_walkthrough_policy__excess_threads_still_trigger_the_existing_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sample(birth: Identity, role: str) -> ProcessSample:
        return ProcessSample(
            identity=birth,
            role="adapter",
            rss_bytes=1,
            cpu_seconds=0.0,
            threads=THREAD_LIMIT + 1,
        )

    def no_limit_change(which: int, limits: tuple[int, int]) -> None:
        return None

    monkeypatch.setattr(guardian, "snapshot", sample)
    monkeypatch.setattr(guardian.resource, "setrlimit", no_limit_change)
    config = Config(
        python_source=str(ROOT),
        purpose="walkthrough",
        repeats=1,
        budget=walkthrough_budget(),
        adapter_command=(sys.executable, "-c", "import time; time.sleep(5)"),
    )
    (tmp_path / "config.json").write_text(config.model_dump_json())
    outcome = guardian.supervise(tmp_path, identity(psutil.Process()))
    assert outcome.status == "failed"
    assert outcome.reason == "Owned thread count budget exceeded"
    assert not outcome.cleanup_survivors
    assert outcome.cleanup_state == "confirmed"
