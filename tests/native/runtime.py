import json
import os
import site
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT.parent / "erlang-python"


def require_runtime(request: pytest.FixtureRequest) -> Path:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and the prebuilt combined canonical runtime")
    option: object = request.config.getoption("hornbeam_runtime")
    if not isinstance(option, str):
        raise TypeError("runtime path must be text")
    profile = Path(option).resolve()
    runtime = profile / "checkouts" / "erlang_python"
    assert (runtime / "ebin" / "py_nif.beam").is_file(), str(runtime)
    assert (runtime / "priv").resolve() == (NATIVE / "priv").resolve()
    return runtime


def host_python_paths(*paths: Path) -> str:
    return json.dumps([*(str(path) for path in paths), *site.getsitepackages()])


def run_launcher(
    runtime: Path, launcher: str, tmp_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ERL_CRASH_DUMP"] = str(tmp_path / "erl_crash.dump")
    return subprocess.run(
        [
            "escript",
            str(ROOT / "tests" / "native" / launcher),
            str(ROOT),
            str(runtime),
            *args,
            "--python-path",
            *site.getsitepackages(),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def run_suites(runtime: Path, suites: tuple[str, ...], tmp_path: Path) -> str:
    compiled = subprocess.run(
        [
            "erlc",
            "-o",
            str(tmp_path),
            *[str(NATIVE / "test" / (suite + ".erl")) for suite in suites],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    expression = """
R = ct:run_test([{dir,"DIRECTORY"}, {suite,[SUITES]},
                 {auto_compile,false}, {logdir,"DIRECTORY"}]),
io:format("NATIVE_CT_RESULT ~p~n", [R]),
halt(case R of {_,0,{0,0}} -> 0; _ -> 1 end).
""".replace("DIRECTORY", str(tmp_path)).replace("SUITES", ",".join(suites))
    environment = os.environ.copy()
    environment["ERL_CRASH_DUMP"] = str(tmp_path / "erl_crash.dump")
    result = subprocess.run(
        [
            "erl",
            "+S",
            "2:2",
            "-noshell",
            "-pa",
            str(tmp_path),
            str(runtime / "ebin"),
            "-eval",
            expression,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    output = result.stdout + result.stderr
    (tmp_path / "native-suites.log").write_text(output)
    assert result.returncode == 0, output
    return output
