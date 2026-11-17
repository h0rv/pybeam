import os
import sys
from pathlib import Path

import pytest

from benchmarks import guardian
from benchmarks.contract import Budget, Config, ProcessSample
from examples.distributed.config import configuration
from examples.distributed.policy import (
    ADMITTED_TASKS,
    CONTEXTS_PER_REQUEST,
    MAIN_CONTEXTS,
    MAX_VMS,
    PROCESS_LIMIT,
    THREAD_LIMIT,
    VM_BASE_THREADS,
    walkthrough_budget,
    walkthrough_stop,
)
from examples.showcase.catalog import ROOT
from pybeam.runtime.ownership import Identity, identity


def test_walkthrough_policy__accounts_for_two_fixed_vm_thread_sets() -> None:
    _, distribution = configuration()
    assert len(distribution.nodes) == MAX_VMS == 2
    assert distribution.capacity == ADMITTED_TASKS == 2
    assert MAIN_CONTEXTS == 1
    assert VM_BASE_THREADS == 18 + 1
    budget = walkthrough_budget()
    assert budget.processes == PROCESS_LIMIT == 16
    assert CONTEXTS_PER_REQUEST == 2
    assert budget.threads == THREAD_LIMIT == 2 * 19 + 2 * 2 + 14 == 56
    assert Budget().threads == 32
    assert budget.wall_seconds == 120.0
    with pytest.raises(ValueError):
        Budget(threads=129)
    with pytest.raises(ValueError):
        Budget(processes=33)


@pytest.mark.parametrize("resource", ["threads", "processes"])
def test_walkthrough_policy__excess_still_triggers_the_existing_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    def sample(birth: Identity, role: str) -> ProcessSample:
        return ProcessSample(
            identity=birth,
            role="adapter",
            rss_bytes=1,
            cpu_seconds=0.0,
            threads=THREAD_LIMIT + 1 if resource == "threads" else 1,
        )

    def no_limit_change(which: int, limits: tuple[int, int]) -> None:
        return None

    def excess_processes(owned: set[Identity]) -> None:
        owned.update(
            Identity(pid=99999999, created=float(index + 1)) for index in range(PROCESS_LIMIT)
        )

    if resource == "processes":
        monkeypatch.setattr(guardian, "discover", excess_processes)
    monkeypatch.setattr(guardian, "snapshot", sample)
    monkeypatch.setattr(guardian.resource, "setrlimit", no_limit_change)
    config = Config(
        python_source=str(ROOT),
        purpose="walkthrough",
        stop_policy=walkthrough_stop(),
        repeats=1,
        budget=walkthrough_budget(),
        adapter_command=(sys.executable, "-c", "import time; time.sleep(5)"),
    )
    (tmp_path / "config.json").write_text(config.model_dump_json())
    stop = tmp_path / "stop.request"
    stop.touch()
    owner = identity(os.getpid())
    outcome = guardian.supervise(
        tmp_path, owner, guardian.Control(owner=owner, stop_file=str(stop))
    )
    assert outcome.status == "failed"
    assert outcome.reason == (
        "Owned thread count budget exceeded"
        if resource == "threads"
        else "Owned process count budget exceeded"
    )
    assert not outcome.cleanup_survivors
    assert outcome.cleanup_state == "confirmed"
