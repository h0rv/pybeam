import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import psutil
import pytest
from pydantic import BaseModel, ConfigDict

from examples.distributed.profile import PROFILE, ROOT, PreparedProfile, verify_prepared
from pybeam.runtime.ownership import Identity, discover, identity, resolve

pytestmark = pytest.mark.runtime


class Guest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    dont_write_bytecode: Literal[True]
    value: Literal[12]
    snapshot: Literal[12]


class RunProof(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

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
    profile = PreparedProfile.model_validate_json(PROFILE.read_bytes())
    assert profile.native_source == ROOT.parent / "native-distribution-ready"
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
            owned = {identity(psutil.Process(process.pid))}
            deadline = time.monotonic() + 30
            while process.poll() is None:
                discover(owned)
                if time.monotonic() >= deadline:
                    (tmp_path / "retained-ownership.json").write_text(
                        "[" + ",".join(value.model_dump_json() for value in owned) + "]"
                    )
                    pytest.fail("Ordinary managed actor deadline; ownership retained, no retry")
                time.sleep(0.01)
        assert process.returncode == 0, log.read_text()
        raw = next(
            line.removeprefix("IMMUTABLE_BUNDLE ")
            for line in log.read_text().splitlines()
            if line.startswith("IMMUTABLE_BUNDLE ")
        )
        guest = Guest.model_validate_json(raw)
        assert all(resolve(value) is None for value in owned)
        assert inventory(runtime) == expected
        verify_prepared()
        proofs.append(
            RunProof(
                run=run, guest=guest, births_gone=tuple(owned), payloads=len(expected), returncode=0
            )
        )
        (tmp_path / ("run-" + str(run) + ".proof.json")).write_text(
            proofs[-1].model_dump_json(indent=2)
        )
    assert len(proofs) == 2
