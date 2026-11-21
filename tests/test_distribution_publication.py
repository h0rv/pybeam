import os
import sys
from pathlib import Path

import pytest

from examples.distributed import jobs
from examples.distributed.config import configuration
from pybeam.codec import Json
from pybeam.distribution import HandlerIdentity, Reply, Request
from pybeam.runtime import cluster as cluster_runtime
from pybeam.runtime.assets import RuntimeAssets
from pybeam.runtime.cluster import NODE_EXIT, LocalCluster, NodeExit
from pybeam.runtime.config import BootstrapError, Config
from pybeam.runtime.ownership import Identity, durable_write, identity


@pytest.fixture
def cluster(tmp_path: Path) -> LocalCluster:
    config, distribution = configuration()
    cluster = LocalCluster(distribution, config=config)
    cluster._directory = tmp_path
    return cluster


def stopped(cluster: LocalCluster, status: int) -> None:
    (cluster.directory / "b.stopped.json").write_bytes(
        NODE_EXIT.encode(
            NodeExit(alias="b", launcher=identity(os.getpid()), clean=True, status=status)
        )
    )


def test_node_check__running_and_unknown_alias(cluster: LocalCluster) -> None:
    cluster.check_running()
    cluster.check_running("b")
    with pytest.raises(ValueError, match="Unknown configured node alias"):
        cluster.check_running("unknown")


@pytest.mark.parametrize("alias", ["a", "b"])
def test_node_check__default_checks_every_node(cluster: LocalCluster, alias: str) -> None:
    (cluster.directory / (alias + ".stopped.json")).write_bytes(
        NODE_EXIT.encode(
            NodeExit(alias=alias, launcher=identity(os.getpid()), clean=False, status=1)
        )
    )
    (cluster.directory / (alias + ".stderr.log")).write_text("failed before identity publication")
    with pytest.raises(BootstrapError) as caught:
        cluster.check_running()
    assert "Node " + alias.upper() + " exited with status 1" in str(caught.value)
    assert "failed before identity publication" in str(caught.value)


def test_node_check__acknowledged_stop_does_not_hide_fresh_failure(cluster: LocalCluster) -> None:
    stopped(cluster, 0)
    cluster.shutdowns.append(NODE_EXIT.decode((cluster.directory / "b.stopped.json").read_bytes()))
    cluster.check_running()
    with pytest.raises(BootstrapError, match="Node B exited with status 0"):
        cluster.check_running("b")
    stopped(cluster, 1)
    with pytest.raises(BootstrapError, match="Node B exited with status 1"):
        cluster.check_running()


def test_node_check__stderr_tail_is_bounded_and_decodes_invalid_utf8(cluster: LocalCluster) -> None:
    stopped(cluster, 1)
    (cluster.directory / "a.stderr.log").write_text("wrong node")
    (cluster.directory / "b.stderr.log").write_bytes(b"discarded" + b"x" * 4096 + b"\xffcause")
    with pytest.raises(BootstrapError) as caught:
        cluster.check_running("b")
    message = str(caught.value)
    assert message.startswith("Node B exited with status 1\nCaptured stderr")
    assert message.endswith("\ufffdcause")
    assert "discarded" not in message
    assert "wrong node" not in message
    assert len(message.split(":\n", 1)[1]) == 4096


@pytest.mark.parametrize("log", ["empty", "missing", "unreadable"])
def test_node_check__stderr_diagnostics_survive_missing_output(
    cluster: LocalCluster, log: str
) -> None:
    stopped(cluster, 1)
    path = cluster.directory / "b.stderr.log"
    if log == "empty":
        path.touch()
    elif log == "unreadable":
        path.mkdir()
    with pytest.raises(BootstrapError) as caught:
        cluster.check_running("b")
    assert "Node B exited with status 1" in str(caught.value)
    assert ("(empty)" if log == "empty" else "Captured stderr unavailable:") in str(caught.value)


def test_node_check__successful_exit_is_not_a_missing_event(cluster: LocalCluster) -> None:
    stopped(cluster, 0)
    with pytest.raises(BootstrapError, match=r"^Node B exited with status 0$"):
        cluster.check_running("b")


class Published(Exception):
    pass


def test_launcher__flushes_traceback_before_publishing_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = cluster_runtime._NodeLaunch(
        entry=tmp_path / "entry.py",
        config=Config(),
        options=(),
        environment={"PYBEAM_CLUSTER_DIRECTORY": str(tmp_path), "PYBEAM_NODE_ALIAS": "b"},
    )
    path = tmp_path / "b.launch.json"
    path.write_bytes(cluster_runtime._NODE_LAUNCH.encode(descriptor))
    log = tmp_path / "b.stderr.log"

    def unavailable(_config: Config) -> RuntimeAssets:
        raise BootstrapError("controlled launcher bootstrap failure")

    def publish(path: Path, text: str) -> None:
        output = log.read_text()
        assert "Traceback (most recent call last):" in output
        assert "BootstrapError: controlled launcher bootstrap failure" in output
        durable_write(path, text)

    monkeypatch.setattr(cluster_runtime, "discover_assets", unavailable)
    monkeypatch.setattr(cluster_runtime, "durable_write", publish)
    with log.open("w") as stderr, monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", stderr)
        with pytest.raises(SystemExit) as caught:
            cluster_runtime._node(path)
        assert caught.value.code == 1
    outcome = NODE_EXIT.decode((tmp_path / "b.stopped.json").read_bytes())
    assert outcome.status == 1 and not outcome.clean


async def test_recipient__identity_and_birth_are_complete_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = HandlerIdentity(node="b@127.0.0.1", pid="<0.1.0>", incarnation="A" * 32)
    published: list[str] = []
    replace = os.replace

    def publish(source: Path, destination: Path) -> None:
        assert not destination.exists()
        if destination.name == "b.identity.json":
            assert Json(HandlerIdentity).decode(source.read_bytes()) == handler
        else:
            assert destination.name == "b.birth.json"
            assert Json(Identity).decode(source.read_bytes()) == identity(os.getpid())
        replace(source, destination)
        published.append(destination.name)

    async def local(request: Request) -> Reply:
        if request.op == "next":
            raise Published
        return Reply(status="ok", identity=handler, max_bytes=4096)

    monkeypatch.setenv("PYBEAM_CLUSTER_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(jobs, "local", local)
    monkeypatch.setattr(os, "replace", publish)
    with pytest.raises(ExceptionGroup) as caught:
        await jobs.serve()
    assert caught.value.subgroup(Published) is not None
    assert published == ["b.identity.json", "b.birth.json"]
