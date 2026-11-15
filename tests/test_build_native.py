import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import build_native


def test_build__caps_existing_hooks_and_uses_selected_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "python"
    config = home / "bin/python3.14-config"
    config.parent.mkdir(parents=True)
    config.touch()
    monkeypatch.setattr(build_native, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "base_prefix", str(home))
    original_run = subprocess.run
    commands: list[list[str]] = []

    def compile(
        argv: list[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(argv)
        assert cwd == tmp_path and check
        assert env["PYTHON_CONFIG"] == str(config)
        assert env["CMAKE_BUILD_PARALLEL_LEVEL"] == "2"
        shim = Path(env["PATH"].split(os.pathsep)[0]) / "getconf"
        result = original_run(
            [str(shim), "_NPROCESSORS_ONLN"], capture_output=True, text=True, check=True
        )
        assert result.stdout == "2\n"
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build_native.subprocess, "run", compile)
    build_native.main()
    assert len(commands) == 1
    assert Path(commands[0][0]).name == "rebar3"
    assert commands[0][1:] == ["as", "hornbeam", "compile"]
    with (tmp_path / "_build/pybeam-build.lock").open("a") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="Another canonical native build"):
            build_native.main()
    assert len(commands) == 1
