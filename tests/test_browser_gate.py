import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect(*options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_discord_demo_browser.py",
            "--collect-only",
            "-q",
            *options,
        ],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_browser_gate__default_deselects_instead_of_claiming_skips() -> None:
    result = collect()
    assert result.returncode == 5, result.stdout + result.stderr
    assert "deselected" in result.stdout
    assert "skipped" not in result.stdout
    assert "test_miniapp__" not in result.stdout


def test_browser_gate__explicit_browser_requires_native_runtime() -> None:
    result = collect("--run-browser")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "--run-browser requires --run-runtime" in result.stderr


def test_browser_gate__explicit_native_browser_collects_real_acceptance() -> None:
    result = collect("--run-runtime", "--run-browser")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_miniapp__two_chrome_sessions_push_ack_race_and_native_restart" in result.stdout
    assert "test_miniapp__nonreading_output_peer_releases_native_subscription" in result.stdout
    assert "skipped" not in result.stdout
