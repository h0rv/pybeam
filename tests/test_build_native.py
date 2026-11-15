import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import build_native


@pytest.mark.parametrize("budget", [None, "", "1", "2"])
def test_build__caps_existing_hooks_and_uses_selected_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str | None
) -> None:
    if budget is None:
        monkeypatch.delenv("CMAKE_BUILD_PARALLEL_LEVEL", raising=False)
    else:
        monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", budget)
    expected = budget or "2"
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
        assert env["CMAKE_BUILD_PARALLEL_LEVEL"] == expected
        assert env["IOMMAP_BUILDOPTS"] == "-j " + expected
        shim = Path(env["PATH"].split(os.pathsep)[0]) / "getconf"
        result = original_run(
            [str(shim), "_NPROCESSORS_ONLN"], capture_output=True, text=True, check=True
        )
        assert result.stdout == expected + "\n"
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


@pytest.mark.parametrize("budget", ["3", "10", "0", "-1", "invalid", "1 2"])
def test_build__rejects_unsafe_budget_before_outputs_or_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    home = tmp_path / "python"
    config = home / "bin/python3.14-config"
    config.parent.mkdir(parents=True)
    config.touch()
    monkeypatch.setattr(build_native, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "base_prefix", str(home))
    monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", budget)
    with pytest.raises(ValueError, match="must be 1 or 2"):
        build_native.main()
    assert not (tmp_path / "_build").exists()


@pytest.mark.parametrize("application", ["erlang_python", "iommap"])
@pytest.mark.parametrize("budget", ["", "1", "2", "10", "invalid"])
def test_project_dependency_override__supplies_bounded_explicit_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, application: str, budget: str
) -> None:
    monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", budget)
    monkeypatch.setenv("IOMMAP_BUILDOPTS", "-j10")
    monkeypatch.setenv("ERL_FLAGS", "+S 1:1 +A 1")
    monkeypatch.setenv("BUDGET_CONFIG", str(build_native.ROOT / "rebar.config"))
    monkeypatch.setenv("BUDGET_APP", application)
    # Use the actual tracked override, not a second copy of the command.
    expression = (
        '{ok, C} = file:consult(os:getenv("BUDGET_CONFIG")), '
        'A = list_to_atom(os:getenv("BUDGET_APP")), '
        "{override, A, O} = lists:keyfind(A, 2, proplists:get_value(overrides, C)), "
        "[{_, compile, Hook}] = proplists:get_value(post_hooks, O), "
        "io:put_chars(Hook), halt()."
    )
    hook = subprocess.check_output(["erl", "-noshell", "-eval", expression], text=True)
    name = "do_iommap.sh" if application == "iommap" else "do_build.sh"
    script = tmp_path / name
    script.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > argv\n')
    script.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", hook], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    if budget in {"", "1", "2"}:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "argv").read_text() == "-j\n" + (budget or "2") + "\n"
    else:
        assert result.returncode != 0
        assert "must be 1 or 2" in result.stderr
        assert not (tmp_path / "argv").exists()
