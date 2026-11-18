import socket
from pathlib import Path

import pytest

from pybeam.runtime import discovery, process
from pybeam.runtime.assets import RuntimeAssets
from pybeam.runtime.config import BootstrapError, Config


def test_cluster__unset_name_keeps_the_node_local() -> None:
    assert discovery.cluster({"PYBEAM_COOKIE": "secret"}, 0.0) is None


@pytest.mark.parametrize("cookie", ["", "x" * 256])
def test_cluster__requires_a_bounded_cookie(cookie: str) -> None:
    with pytest.raises(BootstrapError, match="PYBEAM_COOKIE"):
        discovery.cluster({"PYBEAM_CLUSTER": "localhost", "PYBEAM_COOKIE": cookie}, 0.0)


def test_cluster__names_the_node_by_the_address_that_routes_to_peers() -> None:
    joined = discovery.cluster({"PYBEAM_CLUSTER": "localhost", "PYBEAM_COOKIE": "secret"}, 0.0)
    assert joined is not None
    assert joined.address in {"127.0.0.1", "::1"}
    assert joined.node == "pybeam@" + joined.address
    assert joined.options[:4] == ("-start_epmd", "false", "-erl_epmd_port", "4370")
    assert "secret" not in "".join(joined.options)


def test_cluster__listens_only_on_the_peer_address() -> None:
    ipv6 = discovery.Cluster("app.internal", "fdaa::3").options
    ipv4 = discovery.Cluster("app.internal", "10.0.0.3").options
    assert "inet6_tcp" in ipv6
    assert "{64938,0,0,0,0,0,0,3}" in ipv6
    assert "inet6_tcp" not in ipv4
    assert "{10,0,0,3}" in ipv4


def test_cluster__prefers_ipv4_and_skips_unroutable_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [
        (socket.AF_INET6, socket.SOCK_DGRAM, 0, "", ("::1", 4370, 0, 0)),
        (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("255.255.255.255", 4370)),
        (socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("127.0.0.1", 4370)),
    ]

    def resolve(*_: object, **__: object) -> list[object]:
        return list(answers)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    joined = discovery.cluster({"PYBEAM_CLUSTER": "app.internal", "PYBEAM_COOKIE": "x"}, 0.0)
    assert joined == discovery.Cluster("app.internal", "127.0.0.1")


def test_cluster__waits_for_the_name_until_the_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_: object, **__: object) -> list[object]:
        raise socket.gaierror

    monkeypatch.setattr(socket, "getaddrinfo", missing)
    with pytest.raises(BootstrapError, match=r"does not resolve: app\.internal"):
        discovery.cluster({"PYBEAM_CLUSTER": "app.internal", "PYBEAM_COOKIE": "secret"}, 0.0)


def test_process__cluster_startup_runs_before_the_entry() -> None:
    assets = RuntimeAssets(Path("erl"), ())
    command = process._command(assets, startup=("-eval", "ok."))
    assert command[-5:] == ["-eval", "ok.", "-s", "py_launch", "start"]


def test_cluster__explicit_peers_use_epmd_and_the_first_hosts_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    def address(host: str) -> str:
        queries.append(host)
        return "127.0.0.1"

    monkeypatch.setattr(discovery, "_address", address)
    joined = discovery.cluster(
        {"PYBEAM_CLUSTER": " app@localhost, other@10.0.0.2 ", "PYBEAM_COOKIE": "secret"}, 0.0
    )
    assert joined is not None
    assert queries == ["localhost"]
    assert joined.peers == ("app@localhost", "other@10.0.0.2")
    assert joined.options[:2] == ("-name", "pybeam@127.0.0.1")
    assert "-start_epmd" not in joined.options
    assert "-erl_epmd_port" not in joined.options
    assert "{127,0,0,1}" in joined.options
    assert "secret" not in "".join(joined.options)
    assert "5000" in joined.options[-1]


@pytest.mark.parametrize(
    "nodes",
    [
        "@localhost",
        "app@",
        "app@@localhost",
        "app@localhost,",
        "app@localhost,other",
        "app name@localhost",
    ],
)
def test_cluster__rejects_malformed_peer_lists(nodes: str) -> None:
    with pytest.raises(BootstrapError, match="comma-separated longnames"):
        discovery.cluster({"PYBEAM_CLUSTER": nodes, "PYBEAM_COOKIE": "secret"}, 0.0)


@pytest.mark.parametrize("cookie", ["", "x" * 256])
def test_cluster__explicit_peers_require_a_bounded_cookie(cookie: str) -> None:
    with pytest.raises(BootstrapError, match="PYBEAM_COOKIE"):
        discovery.cluster({"PYBEAM_CLUSTER": "app@localhost", "PYBEAM_COOKIE": cookie}, 0.0)


def test_cluster__unresolved_first_peer_uses_the_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    def missing(host: str) -> None:
        queries.append(host)

    monkeypatch.setattr(discovery, "_address", missing)
    with pytest.raises(BootstrapError, match=r"does not resolve: app@missing\.invalid"):
        discovery.cluster({"PYBEAM_CLUSTER": "app@missing.invalid", "PYBEAM_COOKIE": "secret"}, 0.0)
    assert queries == ["missing.invalid"]


def test_cluster__retries_resolution_before_startup_and_supports_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    pauses: list[float] = []

    def address(host: str) -> str | None:
        attempts.append(host)
        return "::1" if len(attempts) == 2 else None

    monkeypatch.setattr(discovery, "_address", address)
    monkeypatch.setattr(discovery.time, "sleep", pauses.append)
    joined = discovery.cluster({"PYBEAM_CLUSTER": "app@::1", "PYBEAM_COOKIE": "x"}, 10.0)
    assert joined is not None
    assert attempts == ["::1", "::1"]
    assert pauses == [1.0]
    assert joined.node == "pybeam@::1"
    assert "inet6_tcp" in joined.options
    assert "{0,0,0,0,0,0,0,1}" in joined.options


def test_process__cluster_rejects_debug_attach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYBEAM_CLUSTER", "localhost")
    monkeypatch.setenv("PYBEAM_COOKIE", "secret")
    entry = tmp_path / "app.py"
    entry.write_text("")
    assets = RuntimeAssets(Path("erl"), ())
    with pytest.raises(BootstrapError, match="debug attach"):
        process.launch(entry, [], Config(), assets, debug=True)
