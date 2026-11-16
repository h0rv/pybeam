import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from benchmarks import pybeam_reference_build as reference
from benchmarks.contract import Source
from tools import build_native


@pytest.mark.parametrize("entrypoint", ["native", "reference"])
@pytest.mark.parametrize("budget,sanitized", [("1", False), ("1", True), ("2", True)])
def test_build_entry_environment__bounds_real_recursive_make_without_changing_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    budget: str,
    sanitized: bool,
) -> None:
    native = tmp_path / "private"
    for name in ("src/erlang_python.app.src", "priv/py_nif.so", "_build/cmake/CMakeCache.txt"):
        path = native / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("non-C test fixture")
    build = native / "_build/cmake"
    (build / "Makefile").write_text(".PHONY: all\nall:\n\t+$(MAKE) -rR -f assets.mk\n")
    (build / "assets.mk").write_text(
        ".PHONY: all first second\nall: first second\nfirst second:\n"
        "\t@printf 'start-$@\\n' >> events; /bin/sleep 0.2; printf 'end-$@\\n' >> events\n"
    )
    cmake = tmp_path / "fake-cmake"
    cmake.write_text(
        "#!/bin/sh\nset -eu\nrm -f events\n"
        'exec /usr/bin/make -rR -j "$CMAKE_BUILD_PARALLEL_LEVEL"\n'
    )
    cmake.chmod(0o755)
    python = tmp_path / "python"
    python.touch()
    python.with_name("python3.14-config").touch()
    home = tmp_path / "home"
    config = home / "bin/python3.14-config"
    config.parent.mkdir(parents=True)
    config.touch()
    monkeypatch.setattr(sys, "base_prefix", str(home))
    monkeypatch.setattr(build_native.sysconfig, "get_config_var", lambda _name: 0)
    monkeypatch.setattr(build_native, "ROOT", native)
    original_which = shutil.which
    monkeypatch.setattr(
        build_native.shutil,
        "which",
        lambda name: "/mock/rebar3" if name == "rebar3" else original_which(name),
    )
    monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", budget)
    monkeypatch.setenv("IOMMAP_BUILDOPTS", "-j10")
    monkeypatch.setenv("CFLAGS", "preserved-toolchain-sentinel")
    # Keep the old-behavior repro focused on the known nested assignment escape.
    controls = {"MAKEFLAGS": "-kj10 MAKEFLAGS=-j10"}
    if sanitized:
        injection = tmp_path / "injected.mk"
        injection.write_text("override MAKEFLAGS := -j10\n")
        controls.update(
            MFLAGS="-kj",
            GNUMAKEFLAGS="-kj10 MAKEFLAGS=-j10",
            MAKEOVERRIDES="MAKEFLAGS=-j10",
            MAKEFILES=str(injection),
            MAKE="/usr/bin/make -j10",
            MAKELEVEL="99",
        )
    for name in build_native.MAKE_CONTROLS:
        monkeypatch.delenv(name, raising=False)
    for name, value in controls.items():
        monkeypatch.setenv(name, value)
    parent = dict(os.environ)
    if not sanitized:
        # Both rejected helpers copied os.environ directly at this boundary.
        monkeypatch.setattr(build_native, "compiler_environment", lambda: dict(os.environ))
        monkeypatch.setattr(reference, "compiler_environment", lambda: dict(os.environ))
    original_run = subprocess.run
    peaks: list[int] = []
    phases: list[str] = []

    def command(
        argv: Sequence[str],
        *,
        env: dict[str, str],
        check: bool,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        assert check
        assert env["CFLAGS"] == "preserved-toolchain-sentinel"
        assert env["CMAKE_BUILD_PARALLEL_LEVEL"] == budget
        if sanitized:
            assert not build_native.MAKE_CONTROLS.intersection(env)
        if argv[0] == "erlc":
            return subprocess.CompletedProcess(argv, 0)
        # Replace configure/build only. Execute real recursive GNU make under
        # exactly the child environment supplied by the actual Python helper.
        for phase in (
            ("configure", "build")
            if entrypoint == "native"
            else (("build",) if "--build" in argv else ("configure",))
        ):
            result = original_run(
                [str(cmake)],
                cwd=build,
                env=env,
                check=True,
                capture_output=True,
                timeout=timeout,
            )
            active = peak = 0
            events = (build / "events").read_text().splitlines()
            assert len(events) == 4
            for event in events:
                active += 1 if event.startswith("start-") else -1
                peak = max(peak, active)
            assert active == 0
            peaks.append(peak)
            phases.append(phase)
            assert result.returncode == 0
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", command)
    if entrypoint == "native":
        build_native.main()
    else:
        provenance = Source(
            path=str(native),
            sha=reference.BASELINE,
            dirty=False,
            diff_sha256="0" * 64,
            working_tree_sha256="0" * 64,
        )
        monkeypatch.setattr(reference, "source", lambda _path: provenance)
        reference.build(native, python)
    assert phases == ["configure", "build"]
    assert len(peaks) == 2
    if sanitized:
        assert max(peaks) <= int(budget)
    else:
        assert peaks == [2, 2]
    assert dict(os.environ) == parent
    captured = capsys.readouterr()
    if sanitized:
        assert "Inherited make controls replaced" in captured.err
        assert "-kj10" not in captured.err
