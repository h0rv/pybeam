import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest

from pybeam.codec import Json
from pybeam.runtime import guardian
from pybeam.runtime.ownership import Identity, identity, resolve
from pybeam.runtime.process import _stop_owned
from tests.guardian_owner_probe import RaceProof

ROOT = Path(__file__).resolve().parents[1]


class Owned:
    def __init__(self) -> None:
        self.processes: list[tuple[subprocess.Popen[bytes], Identity]] = []

    def spawn(
        self, argv: list[str], *, environment: dict[str, str] | None = None
    ) -> tuple[subprocess.Popen[bytes], Identity]:
        process = subprocess.Popen(
            argv,
            env=environment,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        birth = identity(process.pid)
        self.processes.append((process, birth))
        return process, birth

    def sleeper(self) -> tuple[subprocess.Popen[bytes], Identity]:
        return self.spawn([sys.executable, "-c", "import time; time.sleep(20)"])

    def close(self) -> None:
        for process, birth in reversed(self.processes):
            if process.poll() is None:
                assert resolve(birth) is not None
                _stop_owned(process, 1.0)
            process.wait(timeout=1)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        assert all(resolve(birth) is None for _, birth in self.processes)


@pytest.fixture
def owned() -> Iterator[Owned]:
    commands = Owned()
    try:
        yield commands
    finally:
        commands.close()


@dataclass(frozen=True, slots=True)
class Gate:
    process: subprocess.Popen[bytes]
    birth: Identity
    directory: Path

    @property
    def registration(self) -> Path:
        return self.directory / ("register-" + str(self.birth.pid) + ".json")

    @property
    def marker(self) -> Path:
        return self.directory / "entry"

    def finish(self) -> tuple[int, bytes]:
        _, stderr = self.process.communicate(timeout=7)
        return self.process.wait(timeout=1), stderr


def wait_for(path: Path, gate: Gate) -> None:
    deadline = time.monotonic() + 3
    while not path.exists():
        assert gate.process.poll() is None, "Gate exited before the scheduling boundary"
        assert time.monotonic() < deadline, "Gate did not reach the scheduling boundary"
        time.sleep(0.01)


def start_gate(
    owned: Owned,
    directory: Path,
    owner: Identity,
    guard: Identity,
    *,
    mode: Literal["direct", "before_grant", "after_grant"] = "direct",
    omit_owner: bool = False,
) -> Gate:
    directory.chmod(0o700)
    environment = dict(
        os.environ,
        PYBEAM_OWNER_DIRECTORY=str(directory),
        PYBEAM_OWNER_IDENTITY=Json(type(owner)).encode(owner).decode(),
        PYBEAM_GUARDIAN_IDENTITY=Json(type(guard)).encode(guard).decode(),
    )
    if omit_owner:
        environment.pop("PYBEAM_OWNER_IDENTITY")
    process, birth = owned.spawn(
        [sys.executable, str(ROOT / "tests/guardian_gate_probe.py"), mode], environment=environment
    )
    return Gate(process, birth, directory)


def acknowledge(gate: Gate, *, change: str | None = None) -> guardian.Registration:
    wait_for(gate.registration, gate)
    registration = Json(guardian.Registration).decode(gate.registration.read_text())
    assert registration.process == gate.birth
    updated = registration
    match change:
        case "owner":
            updated = replace(registration, owner=identity(os.getpid()))
        case "guardian":
            updated = replace(registration, guardian=registration.owner)
        case "process":
            updated = replace(
                registration, **{"process": replace(gate.birth, created=gate.birth.created + 1)}
            )
        case "generation":
            updated = replace(
                registration,
                **{"generation": "0" * 32 if registration.generation != "0" * 32 else "1" * 32},
            )
        case None:
            pass
        case _:
            raise ValueError("Unknown acknowledgment mutation")
    guardian.durable_write(
        gate.registration.with_suffix(".ack"), Json(type(updated)).encode(updated).decode()
    )
    return registration


def stop(process: subprocess.Popen[bytes], birth: Identity) -> None:
    assert resolve(birth) is not None
    process.terminate()
    process.wait(timeout=2)
    assert resolve(birth) is None


@pytest.mark.parametrize("subject", ["owner", "guardian"])
def test_guardian__wrong_registration_scope_never_receives_ack(
    owned: Owned, tmp_path: Path, subject: str
) -> None:
    _, owner_birth = owned.sleeper()
    _, guard_birth = owned.sleeper()
    registration = guardian.Registration(
        process=identity(os.getpid()),
        owner=owner_birth,
        guardian=guard_birth,
        generation="0" * 32,
    )
    forged = replace(registration, **{subject: guard_birth if subject == "owner" else owner_birth})
    ack = tmp_path / "registration.ack"
    with pytest.raises(guardian.AuthorizationError, match="different owner or guardian"):
        guardian._acknowledge(ack, forged, owner_birth, guard_birth)
    assert not ack.exists()


def test_guardian__confirmed_owner_gone_never_receives_ack(owned: Owned, tmp_path: Path) -> None:
    owner, owner_birth = owned.sleeper()
    current = identity(os.getpid())
    registration = guardian.Registration(
        process=current, owner=owner_birth, guardian=current, generation="0" * 32
    )
    stop(owner, owner_birth)
    ack = tmp_path / "registration.ack"
    with pytest.raises(guardian.AuthorizationError, match="Owner exited"):
        guardian._acknowledge(ack, registration, owner_birth, current)
    assert not ack.exists()


@pytest.mark.parametrize("baseline", [True, False], ids=["original-rejected", "fixed"])
def test_guardian__real_owner_sigterm_at_exact_ack_boundary(tmp_path: Path, baseline: bool) -> None:
    source = ROOT
    if baseline:
        value = os.environ.get("PYBEAM_OWNERSHIP_BASELINE_SOURCE")
        if value is None:
            pytest.skip("Set an explicit read-only rejected source checkout for the unsafe control")
        source = Path(value)
    environment = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join((str(source / "src"), str(source))),
    )
    output = tmp_path / "proof.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/guardian_owner_probe.py"), "ack_boundary", str(output)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    proof = Json(RaceProof).decode(output.read_text())
    assert proof.source == str(source / "src/pybeam/runtime/guardian.py")
    assert proof.owner_gone_before_resume
    assert proof.executed is baseline
    assert proof.entry_owner_alive is (False if baseline else None)
    assert proof.cleanup.phase == "confirmed"
    assert not proof.survivors
    if not baseline:
        assert not proof.acknowledgment_left


def test_guardian__owner_dead_after_registration_cannot_receive_ack(tmp_path: Path) -> None:
    output = tmp_path / "proof.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/guardian_owner_probe.py"),
            "after_registration",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    proof = Json(RaceProof).decode(output.read_text())
    assert proof.owner_gone_before_resume
    assert not proof.executed
    assert not proof.acknowledgment_left
    assert proof.cleanup.phase == "confirmed"
    assert not proof.survivors


@pytest.mark.parametrize(
    "failure",
    ["owner_gone", "owner_wrong_birth", "guardian_gone", "guardian_wrong_birth", "missing_owner"],
)
def test_gate__early_identity_failures_never_register_or_execute(
    owned: Owned, tmp_path: Path, failure: str
) -> None:
    owner, owner_birth = owned.sleeper()
    guard, guard_birth = owned.sleeper()
    owner_claim, guard_claim = owner_birth, guard_birth
    if failure == "owner_gone":
        stop(owner, owner_birth)
    elif failure == "guardian_gone":
        stop(guard, guard_birth)
    elif failure == "owner_wrong_birth":
        owner_claim = replace(owner_birth, created=owner_birth.created + 1)
        assert resolve(owner_birth) is not None
    elif failure == "guardian_wrong_birth":
        guard_claim = replace(guard_birth, created=guard_birth.created + 1)
        assert resolve(guard_birth) is not None
    gate = start_gate(
        owned, tmp_path, owner_claim, guard_claim, omit_owner=failure == "missing_owner"
    )
    status, _ = gate.finish()
    assert status != 0
    assert not gate.registration.exists()
    assert not gate.marker.exists()


@pytest.mark.parametrize("change", ["owner", "guardian", "process", "generation"])
def test_gate__acknowledgment_is_bound_to_all_identities_and_generation(
    owned: Owned, tmp_path: Path, change: str
) -> None:
    _, owner_birth = owned.sleeper()
    _, guard_birth = owned.sleeper()
    gate = start_gate(owned, tmp_path, owner_birth, guard_birth)
    acknowledge(gate, change=change)
    status, stderr = gate.finish()
    assert status != 0
    assert b"different identity or generation" in stderr
    assert not gate.marker.exists()


@pytest.mark.parametrize("subject", ["owner", "guardian"])
def test_gate__death_after_registration_blocks_ack_consumption(
    owned: Owned, tmp_path: Path, subject: str
) -> None:
    owner, owner_birth = owned.sleeper()
    guard, guard_birth = owned.sleeper()
    gate = start_gate(owned, tmp_path, owner_birth, guard_birth)
    wait_for(gate.registration, gate)
    stop(owner, owner_birth) if subject == "owner" else stop(guard, guard_birth)
    acknowledge(gate)
    status, _ = gate.finish()
    assert status != 0
    assert not gate.marker.exists()


@pytest.mark.parametrize("subject", ["owner", "guardian"])
def test_gate__final_authorization_rechecks_after_matching_ack(
    owned: Owned, tmp_path: Path, subject: str
) -> None:
    owner, owner_birth = owned.sleeper()
    guard, guard_birth = owned.sleeper()
    gate = start_gate(owned, tmp_path, owner_birth, guard_birth, mode="before_grant")
    acknowledge(gate)
    wait_for(tmp_path / "boundary", gate)
    stop(owner, owner_birth) if subject == "owner" else stop(guard, guard_birth)
    (tmp_path / "release").touch()
    status, _ = gate.finish()
    assert status != 0
    assert not gate.marker.exists()


def test_gate__live_owner_and_guardian_authorize_actual_entry(owned: Owned, tmp_path: Path) -> None:
    _, owner_birth = owned.sleeper()
    _, guard_birth = owned.sleeper()
    gate = start_gate(owned, tmp_path, owner_birth, guard_birth)
    acknowledge(gate)
    status, stderr = gate.finish()
    assert status == 0, stderr
    assert gate.marker.read_text() == "live"
    assert not gate.registration.with_suffix(".ack").exists()


def test_gate__post_grant_owner_death_does_not_claim_atomic_preemption(
    owned: Owned, tmp_path: Path
) -> None:
    owner, owner_birth = owned.sleeper()
    _, guard_birth = owned.sleeper()
    gate = start_gate(owned, tmp_path, owner_birth, guard_birth, mode="after_grant")
    acknowledge(gate)
    wait_for(tmp_path / "boundary", gate)
    stop(owner, owner_birth)
    (tmp_path / "release").touch()
    status, stderr = gate.finish()
    assert status == 0, stderr
    assert gate.marker.read_text() == "gone"


def test_gate__missing_ack_is_bounded_and_never_executes(owned: Owned, tmp_path: Path) -> None:
    _, owner_birth = owned.sleeper()
    _, guard_birth = owned.sleeper()
    gate = start_gate(owned, tmp_path, owner_birth, guard_birth)
    status, stderr = gate.finish()
    assert status != 0
    assert b"No guardian ownership acknowledgment" in stderr
    assert not gate.marker.exists()
