import os
import subprocess
from pathlib import Path

import pytest

GUARDS = Path(__file__).parent / "native/genstage_promotion_guards.bash"


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("approval", "approval is required"),
        ("wheel", "hash mismatch"),
        ("environment", "unexpected environment option"),
        ("pipefail", "pipefail is required"),
    ],
)
def test_failed_precondition__aborts_before_any_exchange(
    tmp_path: Path, defect: str, reason: str
) -> None:
    sdk, native = tmp_path / "sdk", tmp_path / "native"
    sdk.mkdir()
    native.mkdir()
    wheel = tmp_path / "bad.whl"
    wheel.write_bytes(b"not the approved wheel")
    marker = tmp_path / "exchange-count"
    reviewed = "explicit-reviewed-sdk"
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path),
        "PYBEAM_GENSTAGE_PROMOTION_APPROVED": "wrong" if defect == "approval" else reviewed,
    }
    if defect == "environment":
        environment["PYBEAM_BUILD_RUNTIME"] = "unexpected-override"
    options = "set -eu" if defect == "pipefail" else "set -euo pipefail"
    command = (
        options + '\nsource "$1"\n'
        'exchange() { printf "exchange\\n" >> "$8"; }\n'
        'genstage_preflight "$2" "$3" "$4" "$5" "$6" "$7"\n'
        'exchange "$@"\n'
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            command,
            "guard-control",
            str(GUARDS),
            str(sdk),
            str(native),
            str(wheel),
            reviewed,
            "explicit-reviewed-native",
            str(native / "py_nif.so"),
            str(marker),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode != 0
    assert reason in result.stderr
    assert not marker.exists(), "An exchange occurred after a failed precondition"


def test_changed_helper_source__aborts_before_any_exchange(tmp_path: Path) -> None:
    native = tmp_path / "native"
    (native / "src").mkdir(parents=True)
    (native / "src/py_application.erl").write_text("unapproved helper source")
    marker = tmp_path / "exchange-count"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail\nsource "$1"\ngenstage_native_helper_hashes "$2"\nprintf exchange > "$3"',
            "helper-control",
            str(GUARDS),
            str(native),
            str(marker),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode != 0
    assert "hash mismatch" in result.stderr
    assert "py_application.erl" in result.stderr
    assert not marker.exists()


def test_failed_pipeline__aborts_before_any_exchange(tmp_path: Path) -> None:
    marker = tmp_path / "exchange-count"
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail\nfalse | true\nprintf "exchange" > "$1"',
            "pipeline-control",
            str(marker),
        ],
        check=False,
        timeout=5,
    )
    assert result.returncode != 0
    assert not marker.exists()
