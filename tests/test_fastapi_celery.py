import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, override

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from examples.fastapi_celery.app import (
    NAMESPACE,
    SLEEP,
    Jobs,
    Status,
    TaskRequest,
    app,
    events,
    running_jobs,
)
from examples.fastapi_celery.compare import Plan, compare, one, pss, sample
from pybeam.submissions import Overloaded
from pybeam.tasks import TaskLimits, TaskRef, TaskSupervisor, _Failure, _Success
from tests.fakes import FakeServices, FakeStore, ServiceState
from tests.test_tasks_dispatch import TasksModule
from tests.test_tasks_dispatch import native as native


@dataclass(slots=True)
class TrackedJobs(Jobs):
    settled: asyncio.Event = field(default_factory=asyncio.Event)

    @override
    async def save(self, status: Status) -> None:
        await super(TrackedJobs, self).save(status)
        if status.task_status != "PENDING":
            self.settled.set()


@asynccontextmanager
async def client_for(
    monkeypatch: pytest.MonkeyPatch, *, fail: bool = False, full: bool = False
) -> AsyncGenerator[tuple[httpx.AsyncClient, TrackedJobs, ServiceState]]:
    state = ServiceState()
    beam = FakeServices(state, owner="test")
    supervisor = TaskSupervisor(TaskLimits(capacity=2, max_bytes=1024, shutdown_ms=100))
    ref = TaskRef[bool](None, None, lambda _: True, lambda: None, 100)

    async def outcome() -> _Success[bool] | _Failure:
        return _Failure(ValueError("broken body")) if fail else _Success(True)

    async def start(_function: object, _seconds: float) -> TaskRef[bool]:
        if full:
            raise Overloaded("full")
        return ref

    monkeypatch.setattr(ref, "_outcome", outcome)
    monkeypatch.setattr(supervisor, "start", start)
    async with asyncio.TaskGroup() as tasks:
        jobs = TrackedJobs(beam, supervisor, tasks, 0.01)

        async def dependency() -> Jobs:
            return jobs

        test_app = FastAPI()
        test_app.include_router(APIRouter(routes=app.routes))
        test_app.dependency_overrides[running_jobs] = dependency
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            yield client, jobs, state


@pytest.mark.parametrize("kind", [1, 2, 3])
async def test_contract_success(monkeypatch: pytest.MonkeyPatch, kind: int) -> None:
    async with client_for(monkeypatch) as (client, jobs, _):
        response = await client.post("/tasks", json={"type": kind})
        assert response.status_code == 201
        task_id = response.json()["task_id"]
        assert isinstance(task_id, str)
        async with asyncio.timeout(1):
            await jobs.settled.wait()
        response = await client.get("/tasks/" + task_id)
        assert response.json() == {
            "task_id": task_id,
            "task_status": "SUCCESS",
            "task_result": True,
        }
        pushed = await client.get("/tasks/" + task_id + "/events")
        assert "SUCCESS" in pushed.text
        assert "event: done" in pushed.text
        assert "hx-sse:close" in (await client.post("/rows", data={"type": "1"})).text


async def test_unknown_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async with client_for(monkeypatch) as (client, _, state):
        assert (await client.get("/tasks/unknown")).json() == {
            "task_id": "unknown",
            "task_status": "PENDING",
            "task_result": None,
        }
        for payload in ({}, {"type": 0}, {"type": 4}, {"type": "1"}, {"type": None}):
            assert (await client.post("/tasks", json=payload)).status_code == 422
        assert not any(call[0] == "publish" for call in state.calls)
        page = (await client.get("/")).text
        assert page.index("htmx.min.js") < page.index("sse.js")
        assert "defer" not in page
        assert (await client.post("/rows", data={"type": "4"})).status_code == 422
        assert (
            await client.post("/rows", data={"type": "1"}, headers={"origin": "http://other"})
        ).status_code == 403


async def test_crash_and_overload(monkeypatch: pytest.MonkeyPatch) -> None:
    async with client_for(monkeypatch, fail=True) as (client, jobs, _):
        response = await client.post("/tasks", json={"type": 1})
        task_id = response.json()["task_id"]
        async with asyncio.timeout(1):
            await jobs.settled.wait()
        assert (await jobs.read(task_id)).task_status == "FAILURE"
    async with client_for(monkeypatch, full=True) as (client, _, _):
        assert (await client.post("/tasks", json={"type": 1})).status_code == 503


async def test_initial_store_failure_releases_slot(
    monkeypatch: pytest.MonkeyPatch, native: TasksModule
) -> None:
    writes = 0
    original_set = FakeStore.set

    async def set_result(
        store: FakeStore, key: str, value: bytes, *, expires_in: float | None = None
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise RuntimeError("store unavailable")
        await original_set(store, key, value, expires_in=expires_in)

    async def outcome(_group: object, _pid: object) -> tuple[Literal[True], bytes]:
        return True, b"true"

    monkeypatch.setattr(FakeStore, "set", set_result)
    monkeypatch.setattr(native, "outcome", outcome)
    beam = FakeServices(ServiceState(), owner="test")
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=1024, shutdown_ms=100)) as supervisor:
        async with asyncio.TaskGroup() as tasks:
            jobs = Jobs(beam, supervisor, tasks, 0.01)
            with pytest.raises(RuntimeError, match="store unavailable"):
                await jobs.submit(TaskRequest(type=1))
        assert len(native.calls) == 1
        ref = await supervisor.start(SLEEP, 0.01)
        assert await ref is True


async def test_events_fresh_read_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    async with client_for(monkeypatch) as (_, jobs, _):
        updates = events("pending", jobs)
        assert "PENDING" in ((await anext(updates)).raw_data or "")
        assert await jobs.beam.subscription_count() == 1
        await jobs.save(Status(task_id="pending", task_status="SUCCESS", task_result=True))
        assert "SUCCESS" in ((await anext(updates)).raw_data or "")
        await updates.aclose()
        assert await jobs.beam.subscription_count() == 0
        assert await jobs.beam.store(NAMESPACE).get("pending") is not None


async def test_compare_empty_and_errors() -> None:
    report = await compare(Plan("http://unused", tasks=0), [])
    assert report.submit_ms is None
    assert report.completion_overhead_ms is None
    assert report.failures == 0

    async def error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(error), base_url="http://test"
    ) as client:
        outcome = await one(client, Plan("http://test"))
    assert outcome.failed
    assert outcome.overhead_ms is None


def test_pss_shared_roots_and_exited_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def descendants(_root: int) -> set[int]:
        return {1, 2, 3}

    def read(path: Path) -> str:
        if path.parts[-2] == "3":
            raise ProcessLookupError
        assert path.name == "smaps_rollup"
        return "Rss: 8192 kB\nPss: 1024 kB\nPss_Dirty: 512 kB\n"

    monkeypatch.setattr("examples.fastapi_celery.compare.tree", descendants)
    monkeypatch.setattr(Path, "read_text", read)
    assert pss([1, 2]) == 2
    assert pss([]) == 0


def test_pss_does_not_hide_unreadable_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    def descendants(_root: int) -> set[int]:
        return {1}

    def read(_path: Path) -> str:
        raise PermissionError("smaps_rollup denied")

    monkeypatch.setattr("examples.fastapi_celery.compare.tree", descendants)
    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError, match="smaps_rollup denied"):
        pss([1])


async def test_memory_peaks(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = iter([(2, 10.0, 1.0), (3, 9.0, 1.5)])
    proportional = iter([4.0, 5.0])
    done = asyncio.Event()

    def usage(_pids: object) -> tuple[int, float, float]:
        snapshot = next(snapshots)
        if snapshot[0] == 3:
            done.set()
        return snapshot

    monkeypatch.setattr("examples.fastapi_celery.compare.usage", usage)
    monkeypatch.setattr("examples.fastapi_celery.compare.pss", lambda _pids: next(proportional))
    report = await sample([1], done)
    assert report is not None
    assert report.peak_pss_mb == 5
    assert report.peak_rss_mb == 10
    assert report.processes == 3
    assert report.cpu_seconds == 0.5
    assert await sample([], done) is None


@pytest.mark.parametrize("status", ["SUCCESS", "FAILURE"])
async def test_compare_success_and_failure(status: str) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"task_id": "test"})
        return httpx.Response(
            200,
            json={
                "task_id": "test",
                "task_status": status,
                "task_result": True if status == "SUCCESS" else "broken",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        outcome = await one(client, Plan("http://test"))
    assert outcome.failed == (status == "FAILURE")
    assert outcome.submit_ms is not None
    assert (outcome.overhead_ms is None) == outcome.failed
