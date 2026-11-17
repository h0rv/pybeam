import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from examples.distributed.profile import PROFILE, ROOT, PreparedProfile, verify_prepared
from pybeam.codec import Json
from pybeam.runtime.ownership import Identity, discover, identity, resolve

pytestmark = pytest.mark.runtime


@dataclass(frozen=True, slots=True, kw_only=True)
class Guest:
    dont_write_bytecode: Literal[True]
    value: Literal[12]
    snapshot: Literal[12]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunProof:
    run: int
    guest: Guest
    births_gone: tuple[Identity, ...]
    payloads: int
    returncode: Literal[0]


def inventory(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_managed_bytecode__two_ordinary_actor_runs_leave_exact_bundle_unchanged(
    tmp_path: Path,
) -> None:
    profile = Json(PreparedProfile).decode(PROFILE.read_bytes())
    assert len(profile.runtime_files) == 723
    runtime = ROOT / "src/pybeam/runtime/_assets"
    verify_prepared()
    expected = inventory(runtime)
    assert expected == profile.runtime_files
    proofs: list[RunProof] = []
    for run in (1, 2):
        environment = dict(os.environ)
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        log = tmp_path / ("run-" + str(run) + ".log")
        with log.open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.fixtures.bytecode_actor"],
                cwd=ROOT,
                env=environment,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            owned = {identity(process.pid)}
            deadline = time.monotonic() + 30
            while process.poll() is None:
                discover(owned)
                if time.monotonic() >= deadline:
                    (tmp_path / "retained-ownership.json").write_text(
                        "["
                        + ",".join(Json(type(value)).encode(value).decode() for value in owned)
                        + "]"
                    )
                    pytest.fail("Ordinary managed actor deadline; ownership retained, no retry")
                time.sleep(0.01)
        assert process.returncode == 0, log.read_text()
        raw = next(
            line.removeprefix("IMMUTABLE_BUNDLE ")
            for line in log.read_text().splitlines()
            if line.startswith("IMMUTABLE_BUNDLE ")
        )
        guest = Json(Guest).decode(raw)
        assert all(resolve(value) is None for value in owned)
        assert inventory(runtime) == expected
        verify_prepared()
        proofs.append(
            RunProof(
                run=run, guest=guest, births_gone=tuple(owned), payloads=len(expected), returncode=0
            )
        )
        (tmp_path / ("run-" + str(run) + ".proof.json")).write_text(
            json.dumps(Json(type(proofs[-1])).dump(proofs[-1]), indent=2)
        )
    assert len(proofs) == 2
