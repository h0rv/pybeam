import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def collect(*options: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_discord_demo_browser.py",
            "--run-frameworks",
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


@pytest.mark.parametrize("explicit", [False, True])
def test_serving_browser_gate__missing_chrome_fails_only_when_requested(
    tmp_path: Path, explicit: bool
) -> None:
    plugin = tmp_path / "missing_chrome.py"
    plugin.write_text(
        "from pathlib import Path\nimport pytest\n"
        "from tests import test_discord_serving as serving\n"
        "serving.CHROME = Path(__file__).with_name('nonexistent-chrome')\n"
        "@pytest.fixture\n"
        "def native_http_server(tmp_path: Path) -> serving.NativeServer:\n"
        "    return serving.NativeServer('http://127.0.0.1:1', tmp_path / 'unused.log')\n"
    )
    entry = tmp_path / "test_missing_chrome.py"
    entry.write_text(
        "from tests.test_discord_serving import "
        "test_uvicorn__two_chrome_contexts_use_managed_native_loop\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--run-frameworks",
            "-p",
            "tests.conftest",
            "-p",
            plugin.stem,
            str(entry),
            "-q",
            *(["--run-runtime", "--run-browser"] if explicit else ["--collect-only"]),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": "",
            "PYTHONPATH": os.pathsep.join((str(tmp_path), str(ROOT))),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (1 if explicit else 5), output
    assert "skipped" not in output
    if explicit:
        assert "1 failed" in output
        assert "--run-browser requested, but Google Chrome is not installed:" in output
        assert "nonexistent-chrome" in output
    else:
        assert "deselected" in output
        assert "test_uvicorn__" not in output


@pytest.mark.parametrize("explicit", [False, True])
def test_showcase_browser_gate__missing_chrome_never_launches_measurements(
    tmp_path: Path, explicit: bool
) -> None:
    plugin = tmp_path / "missing_showcase_chrome.py"
    plugin.write_text(
        "from pathlib import Path\n"
        "from tests import test_showcase_browser as showcase\n"
        "showcase.CHROME = Path(__file__).with_name('nonexistent-chrome')\n"
    )
    entry = tmp_path / "test_missing_showcase_chrome.py"
    entry.write_text(
        "from tests.test_showcase_browser import "
        "test_showcase__actual_chrome_controls_and_evidence\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--run-frameworks",
            "-p",
            "tests.conftest",
            "-p",
            plugin.stem,
            str(entry),
            "-q",
            *(["--run-browser", "--run-benchmarks"] if explicit else ["--collect-only"]),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTEST_ADDOPTS": "",
            "PYTHONPATH": os.pathsep.join((str(tmp_path), str(ROOT))),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (1 if explicit else 5), output
    assert "skipped" not in output
    assert ("--run-browser requested, but Google Chrome is not installed:" in output) is explicit


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


def test_browser_gate__local_showcase_collects_without_native_runtime() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_showcase_browser.py",
            "--collect-only",
            "--run-browser",
            "--run-frameworks",
            "--run-benchmarks",
            "-q",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_showcase__actual_chrome_controls_and_evidence" in result.stdout


def test_browser_gate__explicit_native_browser_collects_real_acceptance() -> None:
    result = collect("--run-runtime", "--run-browser")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_miniapp__two_chrome_sessions_push_ack_race_and_native_restart" in result.stdout
    assert "test_miniapp__nonreading_output_peer_releases_native_subscription" in result.stdout
    assert "skipped" not in result.stdout
