from dataclasses import replace
from pathlib import Path

import pytest

from tests.fixtures.distributed_native_hold import held_demonstration
from tests.fixtures.distributed_native_server import Audit, JobAudit
from tests.test_distributed_active_browser import created_job


def job(key: str, status: str) -> JobAudit:
    return JobAudit(
        id=key,
        status=status,
        directory=Path("fixture-history") / key,
        observations=(),
        proof=None,
        registry=None,
        outcome=None,
    )


def test_active_harness__only_the_authoritative_new_identity_can_match() -> None:
    old = job("completed", "finished")
    new = job("fresh", "running")
    before = frozenset({old.id})
    audit = Audit(subscribers=2, close_calls=0, jobs=(old, new))
    assert created_job(audit, before) is new
    assert created_job(replace(audit, jobs=(old,)), before) is None
    with pytest.raises(ValueError, match="exactly one"):
        created_job(audit, frozenset())


def test_active_harness__the_native_hold_cannot_run_without_a_borrowed_guardian() -> None:
    with pytest.raises(ValueError, match="real borrowed guardian"):
        held_demonstration()
