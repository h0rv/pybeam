import socket
from contextlib import ExitStack
from pathlib import Path

import pytest

from pybeam.distribution import Distribution, LocalCluster, PythonNode
from pybeam.runtime.config import Config
from pybeam.runtime.ownership import resolve

pytestmark = pytest.mark.runtime


def test_pyview_subscription__cross_node_bytes_and_cleanup(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and a built Hornbeam profile")
    profile: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(profile, str)
    entry = Path(__file__).with_name("pyview_pubsub_entry.py")
    distribution = Distribution(
        nodes=(
            PythonNode(alias="a", entry=entry, port=45701),
            PythonNode(alias="b", entry=entry, port=45702),
        ),
        discovery_port=45700,
        capacity=2,
        max_bytes=4096,
    )
    with socket.socket() as listener, ExitStack() as stack:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(30)
        monkeypatch.setenv("PYBEAM_PUBSUB_CONTROL_PORT", str(listener.getsockname()[1]))
        monkeypatch.setenv("PYBEAM_HORNBEAM_SERVICES", "1")
        cluster = LocalCluster(distribution, config=Config(development_runtime=Path(profile)))
        with cluster:
            connections = {}
            for _ in range(2):
                connection, _ = listener.accept()
                stack.enter_context(connection)
                connection.settimeout(5)
                stream = stack.enter_context(connection.makefile("rwb", buffering=0))
                connections[stream.readline()] = stream
            origin, subscriber = connections[b"a\n"], connections[b"b\n"]
            origin.write(b"connect\n")
            assert origin.readline() == b"connected\n"
            for payload in (b"", b"live update", bytes((0, 255, 128))):
                origin.write(payload.hex().encode() + b"\n")
                expected = b"event:" + payload.hex().encode() + b"\n"
                assert {origin.readline(), origin.readline()} == {b"published:2\n", expected}
                assert subscriber.readline() == expected
            for stream in (origin, subscriber):
                stream.write(b"stop\n")
                assert stream.readline() == b"closed\n"
    assert cluster.cleanup is not None and cluster.cleanup.phase == "confirmed"
    assert all(resolve(value) is None for value in cluster.cleanup.owned)
