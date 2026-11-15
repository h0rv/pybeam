import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import override

import psutil
import pytest
from pydantic import ValidationError

from benchmarks import guardian
from benchmarks.contract import Budget, Config, Outcome
from benchmarks.managed import (
    AcknowledgedExit,
    Active,
    AdapterRunning,
    AdapterState,
    Draining,
    FailedManagedShutdown,
    ImmediateCleanup,
    ManagedStopped,
    UnacknowledgedExit,
    read_exit,
)
from benchmarks.runner import summarize
from examples.distributed.policy import walkthrough_budget, walkthrough_stop
from examples.showcase.catalog import ROOT
from pybeam.runtime.cluster import ManagedCleanupAck, NodeExit
from pybeam.runtime.guardian import (
    GuardianEndpoint,
    ImmediateStop,
    Registration,
    StopRequested,
    acknowledge_registered,
)
from pybeam.runtime.ownership import Identity, Registry, identity


def sample_ack(owner: Identity) -> ManagedCleanupAck:
    guard = Identity(pid=900002, created=2.0)
    launcher = Identity(pid=900003, created=3.0)
    return ManagedCleanupAck(
        owner=owner,
        guardian=guard,
        cleanup=Registry(phase="confirmed", owned=(launcher,)),
        launchers=(launcher,),
        shutdowns=(NodeExit(alias="a", launcher=launcher, clean=True, status=143),),
        forced_cleanup_count=0,
    )


def test_managed_stop__typed_transitions_require_an_acknowledged_exit() -> None:
    ack = sample_ack(Identity(pid=900001, created=1.0))
    states: tuple[AdapterState, ...] = (
        AdapterRunning(),
        UnacknowledgedExit("ACK missing"),
        AcknowledgedExit(ack),
    )
    drain = Draining(deadline=1.0)
    for adapter in states:
        assert isinstance(drain.advance(now=1.0, adapter=adapter), FailedManagedShutdown)
    assert drain.advance(now=0.0, adapter=states[0]) is drain
    assert isinstance(drain.advance(now=0.0, adapter=states[1]), FailedManagedShutdown)
    assert drain.advance(now=0.0, adapter=states[2]) == ManagedStopped(ack)
    for reason in ("owner died", "thread budget exceeded", "process budget exceeded"):
        assert Active().abort(reason) == ImmediateCleanup(reason)
        assert drain.abort(reason) == FailedManagedShutdown(reason)


def test_managed_stop__only_nonmeasurement_profile_has_explicit_derived_grace() -> None:
    policy = walkthrough_stop()
    assert policy.grace_seconds == 2 * (5 + 2) + 8 == 22
    assert policy.guardian_grace_seconds == 6
    assert isinstance(Config(python_source=str(ROOT)).stop_policy, ImmediateStop)
    with pytest.raises(ValidationError, match="nonmeasurement"):
        Config(python_source=str(ROOT), stop_policy=policy)


def test_managed_stop__closed_admission_preserves_only_cleanup_lifetime(tmp_path: Path) -> None:
    current = identity(psutil.Process())
    stop = tmp_path / "stop"
    endpoint = GuardianEndpoint(
        directory=tmp_path,
        owner=current,
        guardian=current,
        stop_file=stop,
        stop_policy=walkthrough_stop(),
    )
    endpoint.require_active()
    for marker in (stop, tmp_path / "registration.closed"):
        marker.touch()
        endpoint.require_lifetime()
        with pytest.raises(StopRequested):
            endpoint.require_active()
        registration = tmp_path / "register-denied.json"
        registration.write_text(
            Registration(
                process=current, owner=current, guardian=current, generation="1" * 32
            ).model_dump_json()
        )
        with pytest.raises(StopRequested):
            acknowledge_registered(registration, {current}, endpoint)
        assert not registration.with_suffix(".ack").exists()


def test_managed_stop__ack_binds_owner_guardian_launchers_and_clean_vm_ack(tmp_path: Path) -> None:
    owner = Identity(pid=900001, created=1.0)
    ack = sample_ack(owner)
    owned = {owner, ack.guardian, *ack.cleanup.owned}
    (tmp_path / "managed-cleanup.json").write_text(ack.model_dump_json())
    assert read_exit(tmp_path, owner, owned, 0) == AcknowledgedExit(ack)
    assert isinstance(read_exit(tmp_path, ack.guardian, owned, 0), UnacknowledgedExit)
    assert isinstance(read_exit(tmp_path, owner, {owner}, 0), UnacknowledgedExit)
    assert isinstance(read_exit(tmp_path, owner, owned, 1), UnacknowledgedExit)
    for update in (
        {"forced_cleanup_count": 1},
        {"shutdowns": ()},
        {"cleanup": Registry(phase="uncertain", owned=ack.cleanup.owned)},
    ):
        with pytest.raises(ValidationError):
            ManagedCleanupAck.model_validate_json(ack.model_copy(update=update).model_dump_json())
    outcome = Outcome(
        status="cancelled",
        reason="Managed stop acknowledged",
        returncode=0,
        cleanup_survivors=(),
        cleanup_ns=0,
        forced_cleanup_count=0,
        enforcement=(),
    )
    config = Config(python_source=str(ROOT), purpose="walkthrough", stop_policy=walkthrough_stop())
    summary = summarize(0, tmp_path, config, outcome)
    assert summary.completed is None and summary.throughput_per_second is None
    with pytest.raises(ValueError, match="cooperative"):
        summarize(0, tmp_path, config, outcome.model_copy(update={"forced_cleanup_count": 1}))
    with pytest.raises(ValueError, match="cannot satisfy a benchmark"):
        summarize(0, tmp_path, Config(python_source=str(ROOT)), outcome)


@pytest.mark.parametrize(
    "case", ["legacy", "measurement", "ack", "no_ack", "dead_owner", "timeout"]
)
def test_managed_stop__guardian_waits_only_for_a_managed_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    def no_limit_change(which: int, limits: tuple[int, int]) -> None:
        return None

    def synthetic_exit(
        directory: Path, adapter: Identity, owned: set[Identity], returncode: int
    ) -> AcknowledgedExit | UnacknowledgedExit:
        if (directory / "unit-ack").exists():
            return AcknowledgedExit(sample_ack(adapter))
        return read_exit(directory, adapter, owned, returncode)

    monkeypatch.setattr(guardian.resource, "setrlimit", no_limit_change)
    monkeypatch.setattr(guardian, "read_exit", synthetic_exit)
    if case == "timeout":

        class Expired(Draining):
            @override
            def advance(
                self, *, now: float, adapter: AdapterState
            ) -> Draining | ManagedStopped | FailedManagedShutdown:
                return super().advance(now=self.deadline, adapter=adapter)

        monkeypatch.setattr(guardian, "Draining", Expired)
    stop = tmp_path / "stop.request"
    code = (
        "import sys,time\nfrom pathlib import Path\n"
        "d=Path(sys.argv[1]); (d/'ready').touch()\n"
        "while not (d/'stop.request').exists(): time.sleep(.005)\n"
        + (
            "while not (d/'registration.closed').exists(): time.sleep(.005)\n"
            "(d/'unit-ack').touch()\n"
            if case == "ack"
            else ""
        )
        + ("time.sleep(5)\n" if case in {"legacy", "measurement", "timeout"} else "")
    )
    config = Config(
        python_source=str(ROOT),
        purpose="measurement" if case == "measurement" else "walkthrough",
        stop_policy=ImmediateStop() if case in {"legacy", "measurement"} else walkthrough_stop(),
        budget=Budget() if case == "measurement" else walkthrough_budget(),
        adapter_command=(sys.executable, "-c", code),
    )
    (tmp_path / "config.json").write_text(config.model_dump_json())
    parent = identity(psutil.Process())
    if case == "dead_owner":
        parent = Identity(pid=99999999, created=1.0)
    control = guardian.Control(owner=identity(psutil.Process()), stop_file=str(stop))
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(guardian.supervise, tmp_path, parent, control)
        if case != "dead_owner":
            deadline = time.monotonic() + 5
            while not (tmp_path / "ready").exists():
                assert time.monotonic() < deadline
                time.sleep(0.005)
            stop.touch()
        outcome = result.result(timeout=5)
    if case == "ack":
        assert outcome.status == "cancelled" and outcome.forced_cleanup_count == 0
        assert outcome.cleanup_state == "confirmed"
    elif case in {"no_ack", "timeout"}:
        assert outcome.status == "failed" and outcome.cleanup_state == "uncertain"
    else:
        assert outcome.status == "failed" and outcome.forced_cleanup_count >= 1
    assert not outcome.cleanup_survivors
