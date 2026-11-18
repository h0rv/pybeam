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


def test_cluster__uses_ipv6_distribution_for_ipv6_peers() -> None:
    assert "inet6_tcp" in discovery.Cluster("app.internal", "fdaa::3").options
    assert "inet6_tcp" not in discovery.Cluster("app.internal", "10.0.0.3").options


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
