import json
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol, override
from urllib.parse import urlencode

import httpx
import pytest
from websockets.sync.client import connect

from examples.pyview_cluster.launch import free_port, running

pytestmark = pytest.mark.runtime


class JoinPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token = ""
        self.topic = ""
        self.session = ""

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "csrf-token":
            self.token = values.get("content") or ""
        if tag == "div" and values.get("data-phx-main") == "true":
            self.topic = "lv:" + (values.get("id") or "")
            self.session = values.get("data-phx-session") or ""


class Peer(Protocol):
    def send(self, message: str) -> None: ...

    def recv(self, timeout: float) -> str | bytes: ...


@dataclass
class ViewClient:
    connection: Peer
    topic: str
    ref: int = 1

    def send(self, text: str) -> None:
        self.ref += 1
        self.connection.send(
            json.dumps(
                [
                    "1",
                    str(self.ref),
                    self.topic,
                    "event",
                    {"type": "form", "event": "send", "value": urlencode({"text": text})},
                ]
            )
        )

    def expect(self, text: str) -> str:
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "Expected post did not reach this view"
            reply = self.connection.recv(timeout=remaining)
            assert isinstance(reply, str)
            if text in reply:
                return reply


@contextmanager
def joined(port: int) -> Generator[ViewClient]:
    base = "http://127.0.0.1:" + str(port)
    page = JoinPage()
    response = httpx.get(base + "/", timeout=5)
    response.raise_for_status()
    assert '<title data-suffix="">Two-node feed</title>' in response.text
    assert '<link rel="stylesheet" href="/feed-static/feed.css">' in response.text
    page.feed(response.text)
    assert page.token and page.topic and page.session
    with connect("ws://127.0.0.1:" + str(port) + "/live/websocket", open_timeout=5) as connection:
        connection.send(
            json.dumps(
                [
                    "1",
                    "1",
                    page.topic,
                    "phx_join",
                    {
                        "url": base + "/",
                        "params": {"_csrf_token": page.token},
                        "session": page.session,
                    },
                ]
            )
        )
        reply = connection.recv(timeout=5)
        assert isinstance(reply, str)
        assert '"status": "ok"' in reply, reply
        assert "No posts received" in reply
        yield ViewClient(connection, page.topic)


def wait_ready(ports: tuple[int, int]) -> None:
    with httpx.Client(timeout=0.5) as client:
        for port in ports:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if client.get("http://127.0.0.1:" + str(port) + "/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                raise TimeoutError("Feed did not become ready")


def test_feed__two_nodes_exchange_posts_and_shutdown(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and a built Hornbeam profile")
    profile: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(profile, str)
    inetrc = tmp_path / "inetrc"
    hostname = json.dumps(socket.gethostname().split(".")[0])
    inetrc.write_text(
        "{lookup, [file]}.\n{host, {127,0,0,2}, ["
        + hostname
        + ']}.\n{host, {127,0,0,1}, ["localhost"]}.\n'
    )
    monkeypatch.setenv("ERL_INETRC", str(inetrc))
    ports = free_port(), free_port()
    with (tmp_path / "cluster.log").open("wb") as output:
        for _ in range(2):
            with running(runtime=Path(profile), ports=ports, output=output) as processes:
                wait_ready(ports)
                for port in ports:
                    response = httpx.get("http://127.0.0.1:" + str(port) + "/subscriptions")
                    assert response.status_code == 404
                with joined(ports[0]) as a, joined(ports[1]) as b:
                    a.send("hello from A")
                    assert "A" in b.expect("hello from A")
                    a.expect("hello from A")
                    b.send("hello from B")
                    assert "B" in a.expect("hello from B")
                    b.expect("hello from B")
                    a.send("<script>not markup</script>")
                    assert "&lt;script&gt;" in b.expect("not markup")
                    with joined(ports[0]):
                        pass
            assert all(process.returncode == 0 for process in processes)
            for port in ports:
                with pytest.raises(httpx.TransportError):
                    httpx.get("http://127.0.0.1:" + str(port) + "/health", timeout=0.5)
