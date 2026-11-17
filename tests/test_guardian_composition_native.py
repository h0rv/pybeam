import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from benchmarks.contract import Budget, Config, Outcome
from benchmarks.ownership import Control
from examples.distributed.demo import wait_file
from pybeam.codec import Json
from pybeam.runtime.cluster import LocalCluster
from pybeam.runtime.config import BootstrapError, Distribution, PythonNode
from pybeam.runtime.guardian import GuardianEndpoint, Registration
from pybeam.runtime.ownership import Identity, Registry, cleanup, identity, resolve
from pybeam.runtime.process import _stop_owned

pytestmark = pytest.mark.runtime
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["normal", "adapter_death", "inner_death", "cancel"])
def test_composition__outer_ack_owns_every_executable_birth_without_sampling(
    request: pytest.FixtureRequest, tmp_path: Path, mode: str
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires explicit --run-runtime and prepared assets")
    config = Config(
        python_source=str(ROOT),
        repeats=1,
        adapter_command=(sys.executable, str(ROOT / "tests/guardian_composition_probe.py")),
        budget=Budget(wall_seconds=25.0, processes=32, threads=128, sample_seconds=1.0),
    )
    (tmp_path / "config.json").write_text(config.model_dump_json())
    parent = identity(os.getpid())
    control = Control(owner=parent, stop_file=str(tmp_path / "stop.request"))
    captured: set[Identity] = set()
    with (tmp_path / "guardian.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "benchmarks.guardian",
                str(tmp_path),
                Json(type(parent)).encode(parent).decode(),
                control.model_dump_json(),
            ],
            cwd=ROOT,
            env=dict(os.environ),
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            nested = Json(Registry).decode(wait_file(tmp_path / "nested.json"))
            guard = Json(Identity).decode(wait_file(tmp_path / "inner.birth.json"))
            captured.update((*nested.owned, guard))
            registry = Json(Registry).decode((tmp_path / "ownership.json").read_text())
            assert captured <= set(registry.owned)
            for alias in ("a", "b"):
                birth = Json(Identity).decode(wait_file(tmp_path / (alias + ".entry.json")))
                assert birth in registry.owned and birth in nested.owned
            if mode in {"adapter_death", "inner_death"}:
                target = guard
                if mode == "adapter_death":
                    target = Json(Identity).decode((tmp_path / "adapter.birth.json").read_text())
                victim = resolve(target)
                assert victim is not None
                os.kill(victim.pid, signal.SIGKILL)
            if mode in {"cancel", "inner_death"}:
                (tmp_path / "stop.request").touch()
            else:
                (tmp_path / "release").touch()
            process.wait(timeout=15)
            assert process.returncode == 0, (tmp_path / "guardian.log").read_text()
            outcome = Outcome.model_validate_json((tmp_path / "outcome.json").read_text())
            assert outcome.cleanup_state == "confirmed" and not outcome.cleanup_survivors
            assert all(resolve(birth) is None for birth in captured)
            assert (tmp_path / "registration.closed").is_file()
            if mode == "normal":
                assert outcome.forced_cleanup_count == 0
                assert (
                    Json(Registry).decode((tmp_path / "inner-cleanup.json").read_text()).phase
                    == "confirmed"
                )
        finally:
            if process.poll() is None:
                _stop_owned(process, 1.0)
            survivors = cleanup(captured)
            assert not survivors
            deadline = time.monotonic() + 2
            while any(resolve(birth) is not None for birth in captured):
                assert time.monotonic() < deadline
                time.sleep(0.01)


def test_composition__missing_outer_ack_seals_every_native_entry(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires explicit --run-runtime and prepared assets")
    marker = tmp_path / "entry"
    entry = tmp_path / "node.py"
    entry.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
    current = identity(os.getpid())
    endpoint = GuardianEndpoint(directory=tmp_path, owner=current, guardian=current)
    cluster = LocalCluster(
        Distribution(
            nodes=(
                PythonNode(alias="a", entry=entry, port=45701),
                PythonNode(alias="b", entry=entry, port=45702),
            ),
            discovery_port=45700,
            capacity=1,
            max_bytes=128,
        ),
        guardian=endpoint,
    )
    with pytest.raises(BootstrapError, match="guardian did not start"), cluster:
        pytest.fail("An unacknowledged outer registration must not start nodes")
    assert not marker.exists()
    assert not cluster.identities
    registrations = tuple(tmp_path.glob("register-*.json"))
    assert len(registrations) == 1
    pending = Json(Registration).decode(registrations[0].read_text())
    assert pending.process == cluster._guardian_identity
    assert resolve(pending.process) is None
