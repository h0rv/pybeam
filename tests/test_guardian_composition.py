from pathlib import Path

import psutil
import pytest

from pybeam.runtime import guardian
from pybeam.runtime.guardian import (
    AuthorizationError,
    GuardianEndpoint,
    Registration,
    acknowledge_registered,
    register_borrowed,
)
from pybeam.runtime.ownership import Registry, durable_write, identity


def test_composition__outer_ack_requires_durable_birth_and_revokes_cancelled_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = identity(psutil.Process())
    stop = tmp_path / "stop"
    endpoint = GuardianEndpoint(directory=tmp_path, owner=current, guardian=current, stop_file=stop)
    registration = Registration(
        process=current, owner=current, guardian=current, generation="1" * 32
    )
    path = tmp_path / ("register-" + str(current.pid) + ".json")
    durable_write(path, registration.model_dump_json())

    def write(destination: Path, text: str) -> None:
        durable_write(destination, text)
        if destination.suffix == ".ack":
            registry = Registry.model_validate_json((tmp_path / "ownership.json").read_text())
            assert current in registry.owned
            stop.touch()

    monkeypatch.setattr(guardian, "durable_write", write)
    with pytest.raises(AuthorizationError, match="cancelled"):
        acknowledge_registered(path, {current}, endpoint)
    assert not path.with_suffix(".ack").exists()
    assert path.exists()


@pytest.mark.parametrize("failure", ["cancel", "closed", "owner_birth", "guardian_birth"])
def test_composition__inactive_borrowed_endpoint_never_publishes_registration(
    tmp_path: Path, failure: str
) -> None:
    current = identity(psutil.Process())
    stale = current.model_copy(update={"created": current.created + 1})
    stop = tmp_path / "stop"
    endpoint = GuardianEndpoint(
        directory=tmp_path,
        owner=stale if failure == "owner_birth" else current,
        guardian=stale if failure == "guardian_birth" else current,
        stop_file=stop,
    )
    if failure == "cancel":
        stop.touch()
    if failure == "closed":
        (tmp_path / "registration.closed").touch()
    local = Registration(process=current, owner=current, guardian=current, generation="2" * 32)
    with pytest.raises(AuthorizationError):
        register_borrowed(endpoint, local)
    assert not tuple(tmp_path.glob("register-*.json"))
