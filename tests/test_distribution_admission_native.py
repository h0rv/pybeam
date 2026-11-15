from pathlib import Path

import pytest

from examples.distributed.config import configuration
from examples.distributed.demo import wait_file
from pybeam.distribution import Reply
from pybeam.runtime.cluster import LocalCluster
from pybeam.runtime.config import Distribution, PythonNode
from pybeam.runtime.ownership import Identity, resolve
from tests.distribution_admission_entry import ScopeInfo

pytestmark = pytest.mark.runtime


def test_distribution__shared_ingress_and_request_scope_survive_caller_exit(
    request: pytest.FixtureRequest,
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires explicit --run-runtime and the private paired native build")
    config, _ = configuration()
    entry = Path(__file__).with_name("distribution_admission_entry.py")
    distribution = Distribution(
        nodes=(
            PythonNode(alias="a", entry=entry, port=45701),
            PythonNode(alias="b", entry=entry, port=45702),
        ),
        discovery_port=45700,
        capacity=1,
        max_bytes=4096,
    )
    cluster = LocalCluster(distribution, config=config)
    with cluster:
        result = Reply.model_validate_json(wait_file(cluster.directory / "admission.json"))
        assert result.worker_dead and result.result is not None
        for name in ("scope", "unrelated", "sender-scope"):
            info = ScopeInfo.model_validate_json(wait_file(cluster.directory / (name + ".json")))
            assert info.children == info.contexts == []
            assert info.jobs == info.gate.count == info.gate.bytes == info.gate.unsettled == 0
        births = [
            Identity.model_validate_json(wait_file(cluster.directory / (alias + ".birth.json")))
            for alias in ("a", "b")
        ]
        assert births[0] != births[1]
        cookie = cluster.directory / "home" / ".erlang.cookie"
        assert cookie.stat().st_mode & 0o777 == 0o600
    assert cluster.cleanup is not None and cluster.cleanup.phase == "confirmed"
    assert all(resolve(value) is None for value in cluster.cleanup.owned)
    assert all(exit.clean for exit in cluster.shutdowns)
