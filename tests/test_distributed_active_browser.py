import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest
from playwright.sync_api import APIResponse, Route, expect, sync_playwright

from examples.distributed.policy import walkthrough_stop
from examples.distributed.profile import verify_prepared
from examples.showcase.catalog import ROOT
from pybeam.codec import Json
from pybeam.runtime.cluster import ManagedCleanupAck
from pybeam.runtime.ownership import Registry, identity, resolve
from tests.fixtures.distributed_native_hold import InnerCleanup, NativeReady
from tests.fixtures.distributed_native_server import Audit, JobAudit
from tests.test_distributed_ready_browser import CHROME, wait_audit

pytestmark = [pytest.mark.browser, pytest.mark.runtime]
# Existing HTTP connection drain followed by the bounded managed shutdown.
ACK_DEADLINE = 2 + walkthrough_stop().grace_seconds


def created_job(audit: Audit, previous: frozenset[str]) -> JobAudit | None:
    created = tuple(job for job in audit.jobs if job.id not in previous)
    if len(created) > 1:
        raise ValueError("Expected exactly one authoritative newly created job")
    return next(iter(created), None)


def wait_created(path: Path, previous: frozenset[str]) -> JobAudit:
    deadline = time.monotonic() + 10
    while True:
        job = created_job(Audit.model_validate_json(path.read_bytes()), previous)
        if job is not None:
            return job
        assert time.monotonic() < deadline, "No new authoritative job ID"
        time.sleep(0.01)


def wait_ready(job: JobAudit) -> NativeReady:
    path = job.directory / "result/trial-0/native-ready.json"
    deadline = time.monotonic() + 12
    while not path.is_file():
        assert time.monotonic() < deadline, "No actual two-live-node observer barrier"
        time.sleep(0.01)
    ready = Json(NativeReady).decode(path.read_bytes())
    assert ready.job_id == job.id
    current = Json(Registry).decode(path.with_name("ownership.json").read_bytes())
    assert {node.alias for node in ready.nodes} == {"a", "b"}
    assert ready.nodes[0].birth != ready.nodes[1].birth
    for node in ready.nodes:
        process = resolve(node.birth)
        assert process is not None and psutil.Process(process.pid).exe().endswith("/beam.smp")
        assert node.birth in ready.outer.owned and node.birth in current.owned
    assert ready.exchange.result.value == 42 and ready.exchange.result.side_effect_count == 1
    return ready


@pytest.mark.parametrize("action", ["cancel", "sigterm"])
def test_controlled_native__active_job_identity_cleanup_and_sse(
    tmp_path: Path, action: str
) -> None:
    verify_prepared()
    assert CHROME.is_file()
    audit_path = tmp_path / "audit.json"
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
        (tmp_path / "server.log").open("wb") as log,
    ):
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host, port = Json(tuple[str, int]).load(list(listener.getsockname()))
        base = "http://" + host + ":" + str(port)
        (tmp_path / "private-url.txt").write_text(base)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.fixtures.distributed_native_server",
                str(port),
                str(audit_path),
                "held",
                str(listener.fileno()),
            ],
            cwd=ROOT,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=log,
        )
        server_birth = identity(process.pid)
        (tmp_path / "server-identity.json").write_text(
            Json(type(server_birth)).encode(server_birth).decode()
        )
        try:
            deadline = time.monotonic() + 15
            with httpx.Client(base_url=base, timeout=1) as client:
                while True:
                    assert process.poll() is None, (tmp_path / "server.log").read_text()
                    try:
                        if client.get("/").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(CHROME), headless=True, chromium_sandbox=True
                )
                try:
                    first, second = browser.new_context(), browser.new_context()
                    a, b = first.new_page(), second.new_page()
                    a.goto(base)
                    b.goto(base)
                    audit = wait_audit(audit_path, subscribers=2)
                    previous = frozenset(job.id for job in audit.jobs)
                    held: list[tuple[Route, APIResponse]] = []

                    def delay(route: Route) -> None:
                        held.append((route, route.fetch()))

                    a.route("**/start/distributed", delay)
                    a.get_by_role("button", name=re.compile("^Run Scripted two-node")).click()
                    job = wait_created(audit_path, previous)
                    selector = "#job-" + job.id
                    row_a, row_b = a.locator(selector), b.locator(selector)
                    expect(row_a).to_have_attribute("data-status", "running")
                    expect(row_b).to_have_attribute("data-status", "running")
                    ready = wait_ready(job)
                    (tmp_path / "native-ready.json").write_text(
                        Json(type(ready)).encode(ready).decode()
                    )
                    expect(row_a).to_contain_text(ready.nodes[0].handler.node)
                    expect(row_b).to_contain_text(ready.nodes[1].handler.node)
                    route, reply = held.pop()
                    assert reply.status == 204
                    route.fulfill(response=reply)
                    expect(row_a).to_have_attribute("data-status", "running")
                    assert wait_audit(audit_path, subscribers=2).subscribers == 2
                    if action == "cancel":
                        a.route("**/cancel/" + job.id, delay)
                        row_a.get_by_role("button", name=re.compile("^Cancel")).click()
                        expect(row_a).to_have_attribute(
                            "data-status", "stopped", timeout=int(ACK_DEADLINE * 1000)
                        )
                        expect(row_b).to_have_attribute("data-status", "stopped")
                        route, reply = held.pop()
                        assert reply.status == 204
                        route.fulfill(response=reply)
                        expect(row_a).to_have_attribute("data-status", "stopped")
                    else:
                        # Keep both browser SSE connections open throughout server shutdown.
                        assert resolve(server_birth) is not None
                        assert all(resolve(node.birth) is not None for node in ready.nodes)
                        before = wait_audit(audit_path, subscribers=2)
                        (tmp_path / "before-sigterm.json").write_text(before.model_dump_json())
                        started = time.monotonic()
                        process.terminate()
                        process.wait(timeout=ACK_DEADLINE)
                        (tmp_path / "sigterm-elapsed-seconds.txt").write_text(
                            str(time.monotonic() - started)
                        )
                        assert process.returncode in (0, -15)
                        assert resolve(server_birth) is None
                    audit = Audit.model_validate_json(audit_path.read_bytes())
                    actual = next(item for item in audit.jobs if item.id == job.id)
                    assert actual.outcome is not None and actual.registry is not None
                    assert actual.outcome.status == "cancelled"
                    assert actual.outcome.cleanup_state == "confirmed"
                    assert not actual.outcome.cleanup_survivors
                    assert all(resolve(birth) is None for birth in actual.registry.owned)
                    assert all(resolve(birth) is None for birth in ready.outer.owned)
                    assert actual.outcome.forced_cleanup_count == 0
                    ack = Json(ManagedCleanupAck).decode(
                        (tmp_path / (job.id + ".managed-cleanup.json")).read_bytes()
                    )
                    assert ack.forced_cleanup_count == 0 and len(ack.shutdowns) == 2
                    assert all(item.clean for item in ack.shutdowns)
                    cleanup_path = tmp_path / (job.id + ".inner-cleanup.json")
                    assert cleanup_path.is_file(), "No real inner cleanup/VM shutdown ACK"
                    inner = Json(InnerCleanup).decode(cleanup_path.read_bytes())
                    assert inner.cleanup is not None and inner.cleanup.phase == "confirmed"
                    assert len(inner.shutdowns) == 2 and all(item.clean for item in inner.shutdowns)
                    assert all(resolve(birth) is None for birth in inner.cleanup.owned)
                    if action == "cancel":
                        first.close()
                        second.close()
                        wait_audit(audit_path, subscribers=0)
                        process.terminate()
                        process.wait(timeout=ACK_DEADLINE)
                    audit = wait_audit(audit_path, subscribers=0)
                    assert audit.close_calls == 1
                    assert (
                        next(item for item in audit.jobs if item.id == job.id).status == "stopped"
                    )
                finally:
                    browser.close()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=ACK_DEADLINE)
