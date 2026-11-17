import asyncio

import pytest

from examples.genstage_callbacks import PAYLOAD_LIMIT, Outcome, Work, execute, process
from pybeam.codec import Json


def test_process__should_accept_exact_utf8_boundary_and_reject_one_extra_byte() -> None:
    work = Json(Work)
    prefix = work.encode(Work(id=1, action="fail"))
    payload = work.encode(
        Work(id=1, action="fail", text="é" * ((PAYLOAD_LIMIT - len(prefix)) // 2))
    )
    payload += b" " * (PAYLOAD_LIMIT - len(payload))
    assert len(payload) == PAYLOAD_LIMIT
    assert Json(Outcome).decode(process(payload)).status == "failed"
    with pytest.raises(ValueError, match="exceeds 1024 bytes"):
        process(payload + b" ")


def test_process__should_reject_strict_invalid_boundary_data() -> None:
    with pytest.raises(ValueError):
        process(b'{"id":"1","action":"ok"}')
    with pytest.raises(ValueError):
        process(b'{"id":1,"action":"ok","unexpected":true}')
    with pytest.raises(ValueError):
        process(b"not json")


def test_process__should_reject_oversized_result_without_truncation() -> None:
    payload = Json(Work).encode(Work(id=1, action="ok", text="x" * 980))
    assert len(payload) <= PAYLOAD_LIMIT
    with pytest.raises(ValueError, match="result exceeds"):
        process(payload)


def test_process__should_surface_failure_and_deadline_cancellation() -> None:
    failed = Json(Outcome).decode(process(Json(Work).encode(Work(id=1, action="fail"))))
    cancelled = Json(Outcome).decode(process(Json(Work).encode(Work(id=2, action="cancel"))))
    assert failed.status == "failed"
    assert failed.detail == "deliberate callback failure"
    assert cancelled.status == "cancelled"
    assert cancelled.detail == "application deadline expired"


def test_execute__should_propagate_owner_cancellation_after_cleanup() -> None:
    async def scenario() -> None:
        async with asyncio.TaskGroup() as group:
            task = group.create_task(execute(Work(id=1, action="cancel")))
            await asyncio.sleep(0)
            task.cancel()
        assert task.cancelled()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())
