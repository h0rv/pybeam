import select
import shutil
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel

from tests.conftest import ROOT, ServerFactory

pytestmark = pytest.mark.runtime


class Statistics(BaseModel):
    subscriptions: int
    finalized: int


def statistics(client: httpx.Client) -> Statistics:
    response = client.get("/stats")
    response.raise_for_status()
    return Statistics.model_validate_json(response.content)


@contextmanager
def packet_drop(client_port: int, server_port: int) -> Generator[None]:
    installed: list[list[str]] = []
    try:
        for source, destination in ((client_port, server_port), (server_port, client_port)):
            rule = [
                "OUTPUT",
                "-p",
                "tcp",
                "-s",
                "127.0.0.1",
                "-d",
                "127.0.0.1",
                "--sport",
                str(source),
                "--dport",
                str(destination),
                "-j",
                "DROP",
            ]
            subprocess.run(
                ["sudo", "-n", "iptables", "-w", "5", "-I", *rule],
                check=True,
                capture_output=True,
                timeout=10,
            )
            installed.append(rule)
        yield
    finally:
        try:
            counters = subprocess.run(
                ["sudo", "-n", "iptables", "-w", "5", "-L", "OUTPUT", "-v", "-n", "-x"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            outgoing = "spt:{} dpt:{}".format(server_port, client_port)
            matching = [line for line in counters.stdout.splitlines() if outgoing in line]
            assert len(matching) == 1
            assert int(matching[0].split()[0]) > 0, "cut never dropped server output"
        finally:
            for rule in reversed(installed):
                subprocess.run(
                    ["sudo", "-n", "iptables", "-w", "5", "-D", *rule],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )


def require_packet_filter() -> None:
    if sys.platform != "linux":
        pytest.skip("Real packet DROP requires Linux; SIGKILL cleanup is tested on this platform")
    if shutil.which("sudo") is None or shutil.which("iptables") is None:
        pytest.skip("Real packet DROP requires sudo and iptables")
    permission = subprocess.run(
        ["sudo", "-n", "iptables", "-w", "5", "-L", "OUTPUT", "-n"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if permission.returncode != 0:
        pytest.skip(
            "Real packet DROP requires passwordless sudo and CAP_NET_ADMIN: " + permission.stderr
        )


@pytest.mark.parametrize("scoped", [False, True], ids=["lifespan-owner", "request-owner"])
@pytest.mark.parametrize("publishes", [False, True], ids=["idle", "publishes"])
@pytest.mark.parametrize("cut", ["sigkill", "packet-drop"])
def test_native__sse_disconnect_releases_subscription_and_generator(
    runtime_server_factory: ServerFactory,
    scoped: bool,
    publishes: bool,
    cut: Literal["sigkill", "packet-drop"],
) -> None:
    if cut == "packet-drop":
        require_packet_filter()
    with (
        runtime_server_factory("tests.fixtures.sse_disconnect_app:app") as server,
        subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/native/sse_peer.py"),
                str(server.port),
                str(scoped).lower(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as peer,
    ):
        try:
            assert peer.stdout is not None
            assert select.select([peer.stdout], [], [], 10)[0], "peer never became ready"
            client_port = int(peer.stdout.readline())
            with httpx.Client(base_url=server.base_url, timeout=2) as client:
                assert statistics(client).subscriptions == 1
                if cut == "sigkill":
                    peer.kill()
                    peer.wait(timeout=5)
                    await_cleanup(client, scoped, publishes, cut)
                else:
                    with packet_drop(client_port, server.port):
                        await_cleanup(client, scoped, publishes, cut)
                        assert peer.poll() is None, "network cut must not kill the client"
        finally:
            if peer.poll() is None:
                peer.kill()
            peer.wait(timeout=5)


def await_cleanup(
    client: httpx.Client, scoped: bool, publishes: bool, cut: Literal["sigkill", "packet-drop"]
) -> None:
    started = time.monotonic()
    deadline = started + (55 if cut == "packet-drop" else 25)
    state = statistics(client)
    while time.monotonic() < deadline:
        if publishes:
            client.post("/publish").raise_for_status()
        state = statistics(client)
        if state == Statistics(subscriptions=0, finalized=1):
            print(
                "cut={} scoped={} publishes={} cleaned={:.3f}s state={}".format(
                    cut,
                    scoped,
                    publishes,
                    time.monotonic() - started,
                    state,
                ),
                flush=True,
            )
            return
        time.sleep(0.2)
    pytest.fail(
        "cut={} scoped={} publishes={} retained={:.3f}s state={}".format(
            cut,
            scoped,
            publishes,
            time.monotonic() - started,
            state,
        )
    )
