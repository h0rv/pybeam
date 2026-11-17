import os
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from benchmarks.managed import AcknowledgedExit, Draining, FailedManagedShutdown, read_exit
from pybeam.codec import Json
from pybeam.runtime import guardian
from pybeam.runtime.cluster import ManagedCleanupAck, NodeExit
from pybeam.runtime.guardian import (
    AuthorizationError,
    GuardianEndpoint,
    Registration,
    StopRequested,
    acknowledge_registered,
    observe_vm,
    spawn_owned,
)
from pybeam.runtime.ownership import Identity, Registry, identity
from pybeam.runtime.startup import (
    CompleteStartup,
    LauncherBirth,
    ManagedStartupCensus,
    NoVmStarted,
    PartialStartup,
    PendingLauncher,
    RegisteredLauncher,
    StartupPlan,
    VmEntry,
    progress,
)
from tests.test_managed_stop import sample_ack, sample_census

OWNER = Identity(pid=900001, created=1.0)
LAUNCHER_B = Identity(pid=900005, created=5.0)
VM_B = Identity(pid=900006, created=6.0)


def two_node_census() -> ManagedStartupCensus:
    first = sample_census(OWNER)
    assert isinstance(first.startup, CompleteStartup)
    return ManagedStartupCensus(
        owner=OWNER,
        guardian=first.guardian,
        plan=StartupPlan(aliases=("a", "b")),
        startup=CompleteStartup(
            launches=(
                *first.startup.launches,
                RegisteredLauncher(alias="b", launcher=LAUNCHER_B, vm=VM_B),
            )
        ),
        registry=Registry(phase="confirmed", owned=(*first.registry.owned, LAUNCHER_B, VM_B)),
        forced_cleanup_count=0,
    )


def write_records(
    path: Path, ack: ManagedCleanupAck, census: ManagedStartupCensus
) -> set[Identity]:
    (path / "managed-cleanup.json").write_text(Json(type(ack)).encode(ack).decode())
    (path / "managed-startup-census.json").write_text(Json(type(census)).encode(census).decode())
    return {OWNER, census.guardian, *census.registry.owned}


def full_ack() -> ManagedCleanupAck:
    first = sample_ack(OWNER)
    return ManagedCleanupAck(
        owner=OWNER,
        guardian=first.guardian,
        cleanup=Registry(phase="confirmed", owned=(*first.cleanup.owned, LAUNCHER_B, VM_B)),
        launchers=(*first.launchers, LAUNCHER_B),
        shutdowns=(
            *first.shutdowns,
            NodeExit(alias="b", launcher=LAUNCHER_B, clean=True, status=143),
        ),
        forced_cleanup_count=0,
    )


@pytest.mark.parametrize("omission", ["both_vm_acks", "inner_journal", "one_vm_ack", "alias"])
def test_cleanup_parser__omissions_cannot_change_the_independent_census(
    tmp_path: Path, omission: str
) -> None:
    census = two_node_census()
    ack = full_ack()
    owned = write_records(tmp_path, ack, census)
    assert isinstance(read_exit(tmp_path, OWNER, owned, 0), AcknowledgedExit)
    match omission:
        case "both_vm_acks":
            changed = replace(ack, launchers=(), shutdowns=())
        case "inner_journal":
            changed = replace(
                ack,
                **{
                    "launchers": (),
                    "shutdowns": (),
                    "cleanup": Registry(phase="confirmed", owned=()),
                },
            )
        case "one_vm_ack":
            changed = replace(
                ack, **{"launchers": ack.launchers[:1], "shutdowns": ack.shutdowns[:1]}
            )
        case "alias":
            changed = replace(
                ack,
                **{
                    "shutdowns": (
                        replace(ack.shutdowns[0], **{"alias": "wrong"}),
                        ack.shutdowns[1],
                    )
                },
            )
        case _:
            pytest.fail("Unknown omission control")
    # All controls remain structurally valid ACKs. Only independent completeness rejects them.
    Json(ManagedCleanupAck).decode(Json(type(changed)).encode(changed).decode())
    write_records(tmp_path, changed, census)
    rejected = read_exit(tmp_path, OWNER, owned, 0)
    assert isinstance(Draining(deadline=1).advance(now=0, adapter=rejected), FailedManagedShutdown)


def test_cleanup_parser__census_cannot_omit_a_known_outer_birth(tmp_path: Path) -> None:
    owned = write_records(tmp_path, full_ack(), two_node_census())
    owned.add(Identity(pid=900011, created=11.0))
    result = read_exit(tmp_path, OWNER, owned, 0)
    assert isinstance(Draining(deadline=1).advance(now=0, adapter=result), FailedManagedShutdown)


def test_cleanup_parser__empty_history_requires_an_independent_no_vm_start(tmp_path: Path) -> None:
    service = Identity(pid=900010, created=10.0)
    census = ManagedStartupCensus(
        owner=OWNER,
        guardian=Identity(pid=900002, created=2.0),
        plan=StartupPlan(aliases=("a", "b")),
        startup=NoVmStarted(),
        registry=Registry(phase="confirmed", owned=(service,)),
        forced_cleanup_count=0,
    )
    ack = ManagedCleanupAck(
        owner=OWNER,
        guardian=census.guardian,
        cleanup=Registry(phase="confirmed", owned=(service,)),
        launchers=(),
        shutdowns=(),
        forced_cleanup_count=0,
    )
    owned = write_records(tmp_path, ack, census)
    assert isinstance(read_exit(tmp_path, OWNER, owned, 0), AcknowledgedExit)
    (tmp_path / "managed-startup-census.json").unlink()
    assert isinstance(
        Draining(deadline=1).advance(now=0, adapter=read_exit(tmp_path, OWNER, owned, 0)),
        FailedManagedShutdown,
    )


def test_cleanup_parser__partial_start_requires_every_spawned_launcher(tmp_path: Path) -> None:
    first = sample_census(OWNER)
    assert isinstance(first.startup, CompleteStartup)
    pending = PendingLauncher(alias="b", launcher=LAUNCHER_B)
    launches: tuple[LauncherBirth, ...] = (*first.startup.launches, pending)
    partial = progress(StartupPlan(aliases=("a", "b")), launches)
    assert isinstance(partial, PartialStartup)
    assert isinstance(
        progress(StartupPlan(aliases=("a", "b")), first.startup.launches), PartialStartup
    )
    census = ManagedStartupCensus(
        owner=OWNER,
        guardian=first.guardian,
        plan=StartupPlan(aliases=("a", "b")),
        startup=partial,
        registry=Registry(phase="confirmed", owned=(*first.registry.owned, LAUNCHER_B)),
        forced_cleanup_count=0,
    )
    incomplete = sample_ack(OWNER)
    owned = write_records(tmp_path, incomplete, census)
    assert isinstance(
        Draining(deadline=1).advance(now=0, adapter=read_exit(tmp_path, OWNER, owned, 0)),
        FailedManagedShutdown,
    )
    complete = ManagedCleanupAck(
        owner=OWNER,
        guardian=first.guardian,
        cleanup=census.registry,
        launchers=(*incomplete.launchers, LAUNCHER_B),
        shutdowns=(
            *incomplete.shutdowns,
            NodeExit(alias="b", launcher=LAUNCHER_B, clean=True, status=143),
        ),
        forced_cleanup_count=0,
    )
    write_records(tmp_path, complete, census)
    assert isinstance(read_exit(tmp_path, OWNER, owned, 0), AcknowledgedExit)


def test_startup_observer__closed_admission_cannot_launch_an_early_cancel(tmp_path: Path) -> None:
    (tmp_path / "registration.closed").touch()
    with pytest.raises(StopRequested):
        spawn_owned(tmp_path, ("must-not-run",))
    assert not (tmp_path / "spawn.request.json").exists()
    assert isinstance(progress(StartupPlan(aliases=("a", "b")), ()), NoVmStarted)


def test_startup_observer__census_is_updated_before_registration_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = identity(os.getpid())
    parent = psutil.Process().parent()
    assert parent is not None
    launcher = identity(parent)
    endpoint = GuardianEndpoint(directory=tmp_path, owner=current, guardian=current)
    registration = Registration(
        process=current,
        owner=current,
        guardian=current,
        generation="1" * 32,
        entry=VmEntry(alias="a", launcher=launcher),
    )
    path = tmp_path / "register.json"
    path.write_text(Json(type(registration)).encode(registration).decode())
    launches: list[LauncherBirth] = [PendingLauncher(alias="a", launcher=launcher)]
    events: list[str] = []
    original = guardian._acknowledge

    def observed(value: Registration) -> None:
        observe_vm(launches, value)
        events.append("census")

    def publish(path: Path, value: Registration, owner: Identity, guard: Identity) -> None:
        assert events == ["census"]
        events.append("ack")
        original(path, value, owner, guard)

    monkeypatch.setattr(guardian, "_acknowledge", publish)
    acknowledge_registered(path, {current}, endpoint, before_ack=observed)
    assert events == ["census", "ack"]
    assert launches == [RegisteredLauncher(alias="a", launcher=launcher, vm=current)]


def test_startup_observer__registration_must_match_an_independently_spawned_launcher() -> None:
    launch = PendingLauncher(alias="b", launcher=LAUNCHER_B)
    launches: list[LauncherBirth] = [launch]
    registration = Registration(
        process=VM_B,
        owner=OWNER,
        guardian=Identity(pid=900002, created=2.0),
        generation="1" * 32,
        entry=VmEntry(alias="b", launcher=LAUNCHER_B),
    )
    observe_vm(launches, registration)
    assert launches == [RegisteredLauncher(alias="b", launcher=LAUNCHER_B, vm=VM_B)]
    with pytest.raises(AuthorizationError, match="disagrees"):
        observe_vm(launches, registration)
    with pytest.raises(AuthorizationError, match="independently"):
        observe_vm([], registration)
