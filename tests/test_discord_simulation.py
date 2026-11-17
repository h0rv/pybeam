import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from examples.discord_simulation import Config, Proof
from pybeam.codec import Json
from pybeam.runtime.assets import discover_assets
from pybeam.runtime.config import Config as RuntimeConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "field", ["rooms", "members", "events", "max_inflight", "max_bytes", "history", "seed"]
)
@pytest.mark.parametrize("value", [True, False, 2.0, "2", None, float("nan"), float("inf")])
def test_config__strict_finite_integer_boundary(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        Json(Config).load({field: value})


@pytest.mark.parametrize(
    "data",
    [
        {"rooms": 0},
        {"rooms": 17},
        {"members": 0},
        {"members": 65},
        {"events": 0},
        {"events": 1001},
        {"max_inflight": 0},
        {"max_inflight": 33},
        {"max_bytes": 4095},
        {"max_bytes": 1_048_577},
        {"history": 0},
        {"history": 33},
        {"seed": -1},
        {"seed": 4_294_967_296},
        {"unknown": 1},
    ],
)
def test_config__rejects_out_of_capacity(data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Json(Config).load(data)


@pytest.mark.runtime
@pytest.mark.parametrize(
    "entry",
    [
        "examples/discord_simulation.py",
        "tests/native/discord_simulation.py",
        "tests/native/discord_leave.py",
    ],
)
def test_simulation__managed_native_proof(request: pytest.FixtureRequest, entry: str) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and staged canonical runtime assets")
    discover_assets(RuntimeConfig())
    command = [sys.executable, "-m", entry.removesuffix(".py").replace("/", ".")]
    if entry.startswith("examples/"):
        command += ["--config", '{"rooms":2,"members":3,"events":6,"max_inflight":2}']
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=40, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    if entry.startswith("examples/"):
        proof = next(
            Json(Proof).decode(line) for line in result.stdout.splitlines() if line.startswith("{")
        )
        assert proof.events_before_fault == 6
        assert proof.fanout_receipts_before_fault == 18
        assert len({info.pid for info in proof.room_identities}) == 2
        assert proof.contexts_with_rooms.live == proof.contexts_before.live + 2
        assert proof.before.pid != proof.after.pid
        assert proof.before.generation != proof.after.generation
        assert proof.fresh.members == () and proof.fresh.event_ids == ()
        assert proof.contexts_before.live == proof.contexts_after.live
        assert proof.count_overload_rejections == proof.byte_overload_rejections == 1
    else:
        marker = (
            "discord-leave-native-ok" if entry.endswith("discord_leave.py") else "discord-native-ok"
        )
        assert marker in result.stdout


@pytest.mark.runtime
def test_simulation__installed_wheel_without_source_or_developer_tools(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime")
    wheel = os.environ.get("PYBEAM_TEST_WHEEL")
    if wheel is None:
        pytest.skip("Set PYBEAM_TEST_WHEEL to the built platform wheel")
    uv = shutil.which("uv")
    assert uv is not None
    venv = tmp_path / "venv"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = venv / "bin/python"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(Path(wheel).resolve())],
        check=True,
        capture_output=True,
        text=True,
    )
    app = tmp_path / "discord_simulation.py"
    app.write_text((ROOT / "examples/discord_simulation.py").read_text())
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(python), str(app), "--config", '{"rooms":2,"members":2,"events":4}'],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    proof = next(
        Json(Proof).decode(line) for line in result.stdout.splitlines() if line.startswith("{")
    )
    assert proof.fanout_receipts_before_fault == 8
    assert proof.before.pid != proof.after.pid
    assert proof.contexts_before.live == proof.contexts_after.live
