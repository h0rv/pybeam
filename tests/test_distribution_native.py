import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from examples.distributed.config import configuration
from examples.distributed.demo import demonstration, wait_file
from pybeam.distribution import Reply
from pybeam.runtime.cluster import LocalCluster
from pybeam.runtime.config import BootstrapError, Distribution, PythonNode
from pybeam.runtime.ownership import Identity, Registry, resolve

pytestmark = pytest.mark.runtime
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def require_runtime(request: pytest.FixtureRequest) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires explicit --run-runtime and the private paired native build")


def clean(cluster: LocalCluster) -> None:
    assert cluster.cleanup is not None
    assert cluster.cleanup.phase == "confirmed"
    assert all(resolve(value) is None for value in cluster.cleanup.owned)
    cluster.close()
    cluster.close()


def test_distribution__actual_nodes_handlers_tasks_loss_and_fresh_restart() -> None:
    proof = demonstration()
    for key in ("first", "second"):
        result = Reply.model_validate(proof[key])
        assert result.worker_kind == "temporary_otp_task"
        assert result.worker_dead is True
        assert result.worker is not None and result.worker.startswith("<0.")
        assert result.job_id == key
        assert result.otp_release == "29"
        assert result.otp_version == "29.1.2"
        assert result.erts_version == "17.1.1"
        assert result.private_home is True
    assert Reply.model_validate(proof["inflight_loss"]).status == "uncertain"
    assert Reply.model_validate(proof["controls"]).count == 2
    assert Reply.model_validate(proof["stale_ack"]).status == "stale"
    journal = Registry.model_validate(proof["cleanup"])
    assert journal.phase == "confirmed"
    assert all(resolve(value) is None for value in journal.owned)


def test_distribution__wrong_cookie_never_produces_success_receipt() -> None:
    config, distribution = configuration()
    cluster = LocalCluster(distribution, config=config)
    with cluster:
        wait_file(cluster.directory / "first.json")
        with pytest.raises(RuntimeError, match="only once"):
            cluster.__enter__()
        epmd = resolve(cluster.identities[0])
        assert epmd is not None and epmd.exe().endswith("/epmd")
        discovery_listeners = [
            connection
            for connection in epmd.net_connections(kind="inet")
            if connection.status == psutil.CONN_LISTEN
        ]
        assert discovery_listeners
        for connection in discovery_listeners:
            assert connection.laddr.ip in {"127.0.0.1", "::1"}
            assert connection.laddr.port == distribution.discovery_port
        for alias in ("a", "b"):
            birth = Identity.model_validate_json(
                (cluster.directory / (alias + ".birth.json")).read_text()
            )
            vm = resolve(birth)
            assert vm is not None and vm.exe().endswith("/beam.smp")
            listeners = [
                connection
                for connection in vm.net_connections(kind="inet")
                if connection.status == psutil.CONN_LISTEN
            ]
            assert listeners
            for connection in listeners:
                assert connection.laddr.ip == "127.0.0.1"
                assert connection.laddr.port in {node.port for node in distribution.nodes}
        cookie = cluster.directory / "home" / ".erlang.cookie"
        assert cookie.stat().st_mode & 0o777 == 0o600
        cluster.stop("b")
        cookie.write_text("a_separate_owned_cookie_for_a_finite_negative_control")
        (cluster.directory / "b.identity.json").unlink()
        cluster.restart("b")
        wait_file(cluster.directory / "b.identity.json")
        with pytest.raises(RuntimeError, match="one restart"):
            cluster.restart("b")
        (cluster.directory / "try-join").touch()
        refused = Reply.model_validate_json(wait_file(cluster.directory / "join-control.json"))
        assert refused.status == "uncertain"
        assert refused.receipt is None
    clean(cluster)


def test_distribution__partial_startup_is_owned_before_any_entry_code(tmp_path: Path) -> None:
    config, _ = configuration()
    marker = tmp_path / "entry-owned.json"
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import os, psutil\nfrom pathlib import Path\n"
        "from pybeam.runtime.ownership import identity, Registry\n"
        "directory = Path(os.environ['PYBEAM_OWNER_DIRECTORY'])\n"
        "registry = Registry.model_validate_json((directory / 'ownership.json').read_text())\n"
        "assert identity(psutil.Process()) in registry.owned\n"
        + "Path("
        + repr(str(marker))
        + ").write_text(identity(psutil.Process()).model_dump_json())\n"
        + "raise RuntimeError('finite controlled entry failure')\n"
    )
    idle = tmp_path / "idle.py"
    idle.write_text(
        "import asyncio\nfrom pybeam import run\nasync def main():\n    await asyncio.sleep(30)\nrun(main)\n"
    )
    distribution = Distribution(
        nodes=(
            PythonNode(alias="a", entry=idle, port=45701),
            PythonNode(alias="b", entry=bad, port=45702),
        ),
        discovery_port=45700,
        capacity=2,
        max_bytes=4096,
    )
    cluster = LocalCluster(distribution, config=config.model_copy(update={"startup_timeout": 3.0}))
    try:
        with cluster:
            value = Identity.model_validate_json(wait_file(marker, seconds=5))
            deadline = time.monotonic() + 5
            while resolve(value) is not None:
                assert time.monotonic() < deadline
                time.sleep(0.02)
    except BootstrapError:
        pass
    assert marker.exists(), "Failure must occur only after guardian-owned entry execution"
    clean(cluster)


def test_distribution__registration_failure_seals_user_execution(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    command = [
        sys.executable,
        "-m",
        "pybeam.runtime.guardian",
        "gate",
        sys.executable,
        "-c",
        "from pathlib import Path; Path(" + repr(str(marker)) + ").touch()",
    ]
    result = subprocess.run(
        command,
        env=dict(os.environ, PYBEAM_OWNER_DIRECTORY=str(tmp_path)),
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode != 0
    assert "No guardian ownership acknowledgment" in result.stderr
    assert not marker.exists()


def test_distribution__parent_exit_reaps_private_epmd_and_both_native_vms() -> None:
    code = (
        "import os\nfrom examples.distributed.config import configuration\n"
        "from examples.distributed.demo import wait_file\n"
        "from pybeam.runtime.cluster import LocalCluster\n"
        "config, distribution = configuration()\n"
        "cluster = LocalCluster(distribution, config=config)\n"
        "cluster.__enter__()\nwait_file(cluster.directory / 'first.json')\n"
        "print(str(cluster.directory), flush=True)\nos._exit(0)\n"
    )
    parent = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert parent.returncode == 0, parent.stderr
    directory = Path(parent.stdout.strip())
    assert directory.name.startswith("pybeam-distribution-")
    journal = Registry.model_validate_json((directory / "ownership.json").read_text())
    assert journal.phase == "confirmed"
    assert all(resolve(value) is None for value in journal.owned)
    assert not (directory / "home" / ".erlang.cookie").exists()
    shutil.rmtree(directory)


def test_distribution__guardian_exit_closes_live_vm_execution() -> None:
    config, distribution = configuration()
    cluster = LocalCluster(distribution, config=config)
    with pytest.raises(BootstrapError), cluster:
        wait_file(cluster.directory / "first.json")
        births = [
            Identity.model_validate_json((cluster.directory / (alias + ".birth.json")).read_text())
            for alias in ("a", "b")
        ]
        guardian = cluster._guardian
        assert guardian is not None
        guardian.terminate()
        guardian.wait(timeout=8)
        deadline = time.monotonic() + 8
        while any(resolve(value) is not None for value in births):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    clean(cluster)
