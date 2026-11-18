import asyncio
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from benchmarks.contract import Budget, Config, Outcome
from benchmarks.runner import summarize
from examples.distributed import demo
from examples.distributed.demo import demonstration
from examples.distributed.models import (
    OBSERVATION,
    NodeObserved,
    Observation,
    WalkthroughProof,
    exchange,
    sanitized,
)
from examples.showcase import distributed
from examples.showcase.app import create_app
from examples.showcase.catalog import ROOT, catalog
from examples.showcase.jobs import Job, Jobs
from pybeam.codec import Json
from pybeam.distribution import Distribution
from pybeam.runtime.cluster import NODE_EXIT, NodeExit
from pybeam.runtime.config import Config as RuntimeConfig
from pybeam.runtime.guardian import AuthorizationError, GuardianEndpoint
from pybeam.runtime.ownership import Identity, identity
from tests.fixtures.distributed_cluster import ControlledCluster
from tests.showcase_fixtures import controlled_proof, controlled_walkthrough

BASE = "http://127.0.0.1:38921"


def test_gate__does_not_accept_unreviewed_native_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("examples.distributed.profile.PROFILE", tmp_path / "missing.json")
    card = next(card for card in catalog(trusted_native_root=tmp_path) if card.key == "distributed")
    assert card.disabled is not None
    assert "prepared distribution profile" in card.disabled
    with TestClient(create_app(), base_url=BASE) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/chat"
        page = client.get("/?scenario=distributed").text
        assert "Scripted two-node walkthrough" in page
        assert 'hx-sse:connect="/events"' in page
        assert "every 1s" not in page
        assert 'id="job-refresh"' in page
        assert client.post("/start/distributed", headers={"origin": BASE}).status_code == 409
        assert client.get("/artifact/../../etc/passwd").status_code == 404


def test_projection__only_allowlisted_receipt_fields() -> None:
    proof = controlled_proof()
    public = sanitized(proof)
    text = Json(type(public)).encode(public).decode()
    assert "SECRET" in Json(type(proof)).encode(proof).decode()
    for forbidden in (
        "SECRET",
        "token",
        "payload",
        "directory",
        "guardian",
        "cookie",
        str(Path("/") / "tmp"),
    ):
        assert forbidden not in text
    assert Json(WalkthroughProof).decode(text) == public
    with pytest.raises(ValueError):
        OBSERVATION.decode('{"kind":"state","state":"fresh_empty","cookie":"secret"}')


@pytest.mark.parametrize("field", ["node", "pid", "incarnation", "message_id"])
def test_projection__rejects_private_paths_disguised_as_identity(field: str) -> None:
    proof = controlled_proof()
    handler = proof.first.recipient
    assert handler is not None
    with pytest.raises(ValueError):
        NodeObserved(
            alias="b",
            birth=proof.vm_b,
            status="observed",
            handler=replace(handler, **{field: "/private/secret-cookie"}),
        )


def test_receipts__rejects_misrouted_ack_result_and_handler_as_task() -> None:
    proof = controlled_proof()
    assert proof.first.recipient is not None
    for broken in (
        replace(proof.first, worker=proof.first.recipient.pid),
        replace(
            proof.first,
            **{"result": '{"value":42,"side_effect_count":2,"python_version":"3.14.10"}'},
        ),
        replace(proof.first, reverse_ack=proof.second.reverse_ack),
    ):
        with pytest.raises(ValueError):
            exchange(broken)


def test_proof__requires_fresh_births_and_confirmed_cleanup() -> None:
    proof = controlled_proof()
    for broken in (
        replace(proof, restarted_b=proof.vm_b),
        replace(proof, fresh=proof.first),
        replace(proof, **{"cleanup": replace(proof.cleanup, phase="uncertain")}),
    ):
        with pytest.raises(ValueError):
            sanitized(broken)


def test_runner__walkthrough_is_not_measurement(tmp_path: Path) -> None:
    walkthrough = controlled_walkthrough()
    (tmp_path / "adapter.json").write_text(Json(type(walkthrough)).encode(walkthrough).decode())
    outcome = Outcome(
        status="walkthrough",
        reason=None,
        returncode=0,
        cleanup_survivors=(),
        cleanup_ns=0,
        forced_cleanup_count=0,
        enforcement=(),
    )
    config = Config(python_source=str(ROOT), purpose="walkthrough", repeats=1, budget=Budget())
    summary = summarize(0, tmp_path, config, outcome)
    assert summary.completed is None
    assert summary.throughput_per_second is None
    assert summary.latency_ns is None
    assert summary.simultaneous_tree_rss_peak_bytes is None
    with pytest.raises(ValueError, match="cannot satisfy a benchmark"):
        summarize(0, tmp_path, config.model_copy(update={"purpose": "measurement"}), outcome)
    with pytest.raises(ValueError, match="cannot satisfy a walkthrough"):
        summarize(0, tmp_path, config, outcome.model_copy(update={"status": "passed"}))


async def test_subscribers__bounded_coalesced_fresh_and_disconnect(tmp_path: Path) -> None:
    async with asyncio.TaskGroup() as group:
        jobs = Jobs(group, tmp_path)
        subscribers = tuple(jobs.subscribe() for _ in range(16))
        with pytest.raises(ValueError, match="16 event streams"):
            jobs.subscribe()
        for _ in range(100):
            jobs.changed()
        assert all(queue.qsize() == 1 for queue in subscribers)
        assert await subscribers[0].get() == 100
        for queue in subscribers:
            jobs.unsubscribe(queue)
        assert not jobs.subscribers
        queue = jobs.subscribe()
        assert await queue.get() == 100
        jobs.unsubscribe(queue)
        await jobs.close()


async def test_cancel__stale_snapshot_cannot_revive_terminal_job(tmp_path: Path) -> None:
    async with asyncio.TaskGroup() as group:
        jobs = Jobs(group, tmp_path)
        old = Job("owned", catalog()[0], tmp_path, status="running")
        jobs.items[old.id] = old
        jobs.stop(old)
        revision = jobs.revision
        jobs.stop(old)
        assert jobs.revision == revision
        terminal = replace(jobs.items[old.id], status="stopped")
        jobs.update(terminal)
        jobs.stop(old)
        assert jobs.items[old.id] == terminal
        with pytest.raises(FrozenInstanceError):
            old.__setattr__("status", "running")
        await jobs.close()


def test_adapter__requires_live_borrowed_guardian_before_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(distributed, "distributed_unavailable", lambda: None)
    current = identity(os.getpid())
    endpoint = GuardianEndpoint(
        directory=tmp_path, owner=current, guardian=Identity(pid=99999999, created=1.0)
    )
    with pytest.raises(AuthorizationError):
        distributed.walkthrough(tmp_path, endpoint)
    assert not (tmp_path / "adapter.json").exists()
    assert not (tmp_path / "observations.jsonl").exists()


def test_demo__controlled_receipt_boundary_callback_and_cli_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, distribution = demo.configuration()
    cluster = ControlledCluster(distribution, config=config, directory=tmp_path)

    def factory(
        distribution: Distribution,
        *,
        config: RuntimeConfig | None = None,
        guardian: GuardianEndpoint | None = None,
    ) -> ControlledCluster:
        return cluster

    monkeypatch.setattr(demo, "LocalCluster", factory)
    observed: list[Observation] = []
    path = tmp_path / "sanitized.json"
    full = demonstration(observe=observed.append, output=path)
    assert "first" in full and "cleanup" in full and "shutdowns" in full
    assert Json(WalkthroughProof).decode(path.read_text()).observations == tuple(observed)
    assert cluster.starts == 1 and cluster.restarts == 1 and cluster.closed
    assert "SECRET" not in path.read_text()


@pytest.mark.parametrize("status", [0, 7])
def test_demo__reports_sender_exit_instead_of_missing_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    config, distribution = demo.configuration()
    cluster = ControlledCluster(distribution, config=config, directory=tmp_path)
    (tmp_path / "a.stopped.json").write_bytes(
        NODE_EXIT.encode(
            NodeExit(alias="a", launcher=identity(os.getpid()), clean=True, status=status)
        )
    )

    def factory(
        distribution: Distribution,
        *,
        config: RuntimeConfig | None = None,
        guardian: GuardianEndpoint | None = None,
    ) -> ControlledCluster:
        return cluster

    monkeypatch.setattr(demo, "LocalCluster", factory)
    with pytest.raises(RuntimeError, match="Node A exited with status " + str(status)):
        demonstration()
    assert cluster.closed


def test_adapter__controlled_boundary_writes_typed_adapter_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, distribution = demo.configuration()
    cluster = ControlledCluster(distribution, config=config, directory=tmp_path)

    def factory(
        distribution: Distribution,
        *,
        config: RuntimeConfig | None = None,
        guardian: GuardianEndpoint | None = None,
    ) -> ControlledCluster:
        assert guardian is endpoint
        return cluster

    current = identity(os.getpid())
    endpoint = GuardianEndpoint(directory=tmp_path, owner=current, guardian=current)
    monkeypatch.setattr(demo, "LocalCluster", factory)
    monkeypatch.setattr(distributed, "distributed_unavailable", lambda: None)
    distributed.walkthrough(tmp_path, endpoint)
    proof = Json(WalkthroughProof).decode((tmp_path / "adapter.json").read_text())
    events = tuple(
        OBSERVATION.decode(line)
        for line in (tmp_path / "observations.jsonl").read_text().splitlines()
    )
    assert proof.observations == events
    assert cluster.closed


def test_demo__controlled_cancellation_after_receipt_closes_without_success_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, distribution = demo.configuration()
    cluster = ControlledCluster(distribution, config=config, directory=tmp_path)

    def factory(
        distribution: Distribution,
        *,
        config: RuntimeConfig | None = None,
        guardian: GuardianEndpoint | None = None,
    ) -> ControlledCluster:
        return cluster

    def cancelled(event: Observation) -> None:
        raise AuthorizationError("Controlled cancellation after receipt")

    monkeypatch.setattr(demo, "LocalCluster", factory)
    output = tmp_path / "sanitized.json"
    with pytest.raises(AuthorizationError, match="after receipt"):
        demonstration(observe=cancelled, output=output)
    assert cluster.closed
    assert cluster.restarts == 0
    assert not output.exists()


def test_demo__cancellation_before_entry_has_no_observations() -> None:
    def cancelled() -> None:
        raise AuthorizationError("Controlled cancellation before entry")

    with pytest.raises(AuthorizationError, match="before entry"):
        demonstration(check=cancelled)
