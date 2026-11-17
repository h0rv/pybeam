import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from pybeam.codec import Json
from pybeam.runtime import guardian
from pybeam.runtime.ownership import Identity, Registry, identity, resolve
from pybeam.runtime.process import _stop_owned


@dataclass(frozen=True, slots=True, kw_only=True)
class RaceProof:
    source: str
    boundary: str
    owner: Identity
    owner_gone_before_resume: bool
    executed: bool
    entry_owner_alive: bool | None
    acknowledgment_left: bool
    cleanup: Registry
    survivors: tuple[Identity, ...]
    failure: str | None


def probe(boundary: str) -> RaceProof:
    with tempfile.TemporaryDirectory(prefix="pybeam-owner-authorization-") as temporary:
        directory = Path(temporary)
        (directory / "home").mkdir(mode=0o700)
        os.environ["PYBEAM_OWNER_DIRECTORY"] = str(directory)
        marker = directory / "entry"
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(12)"], start_new_session=True
        )
        owner_birth = identity(owner.pid)
        entry = (
            "from pathlib import Path; from pybeam.runtime.ownership import Identity, resolve; "
            "owner=Json(Identity).decode("
            + repr(Json(type(owner_birth)).encode(owner_birth).decode())
            + "); "
            "Path("
            + repr(str(marker))
            + ").write_text('live' if resolve(owner) is not None else 'gone')"
        )
        request = guardian.SpawnRequest(argv=(sys.executable, "-c", entry))
        guardian.durable_write(
            directory / "spawn.request.json", Json(type(request)).encode(request).decode()
        )
        original_write = guardian.durable_write
        original_record = guardian.record
        owner_gone = False
        tracking = 0
        failure: str | None = None

        def terminate_owner() -> None:
            nonlocal owner_gone
            owner.terminate()
            owner.wait(timeout=2)
            owner_gone = resolve(owner_birth) is None
            if not owner_gone:
                raise AssertionError("Owner birth identity still exists at the scheduling barrier")

        def write(path: Path, text: str) -> None:
            if boundary == "ack_boundary" and path.suffix == ".ack":
                terminate_owner()
                original_write(path, text)
                deadline = time.monotonic() + 1
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            else:
                original_write(path, text)

        def record(
            path: Path,
            owned: set[Identity],
            phase: Literal["tracking", "spawning", "confirmed", "uncertain"],
        ) -> None:
            nonlocal tracking
            original_record(path, owned, phase)
            if phase == "tracking" and owned:
                tracking += 1
                if boundary == "after_registration" and tracking == 2:
                    terminate_owner()

        try:
            with (
                patch.object(guardian, "durable_write", write),
                patch.object(guardian, "record", record),
            ):
                try:
                    guardian.supervise(directory, owner_birth)
                except RuntimeError as error:
                    failure = type(error).__name__
            journal = Json(Registry).decode((directory / "ownership.json").read_text())
            return RaceProof(
                source=str(guardian.__file__),
                boundary=boundary,
                owner=owner_birth,
                owner_gone_before_resume=owner_gone,
                executed=marker.exists(),
                entry_owner_alive=(marker.read_text() == "live") if marker.exists() else None,
                acknowledgment_left=any(directory.glob("*.ack")),
                cleanup=journal,
                survivors=tuple(value for value in journal.owned if resolve(value) is not None),
                failure=failure,
            )
        finally:
            if owner.poll() is None:
                _stop_owned(owner, 1.0)


if __name__ == "__main__":
    result = probe(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(Json(type(result)).dump(result), indent=2) + "\n")
    print(
        json.dumps(
            {
                "executed": result.executed,
                "owner_alive_at_entry": result.entry_owner_alive,
                "cleanup": result.cleanup.phase,
                "survivors": len(result.survivors),
            }
        )
    )
