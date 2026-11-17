import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import assert_type

import pytest

from pybeam.codec import CodecError, Json
from pybeam.submissions import CapabilityUnavailable
from pybeam.tasks import ExecutionMode, TaskFunction, TaskLimits, TaskRef, TaskSupervisor
from tests.typechecking import TYPECHECK_CONFIG


@dataclass(frozen=True, slots=True)
class Request:
    value: int


async def render(request: Request) -> str:
    return str(request.value)


async def checked(supervisor: TaskSupervisor) -> None:
    function = TaskFunction(render, Json(Request), Json(str))
    ref = await supervisor.start(function, Request(value=21))
    assert_type(ref, TaskRef[str])
    assert_type(function.execution, ExecutionMode)
    assert_type(await ref, str)
    assert_type(await ref.result(), str)
    assert_type(await ref.exception(), BaseException | None)
    assert_type(await ref.cancel(), bool)


def test_execution_mode__values() -> None:
    assert {mode.name: mode.value for mode in ExecutionMode} == {
        "WORKER": "worker",
        "OWNGIL": "owngil",
        "ISOLATED": "isolated",
    }


@pytest.mark.parametrize("dedicated_loop", [False, True])
@pytest.mark.parametrize("execution", [ExecutionMode.WORKER, "worker"])
def test_task_function__worker_default_and_explicit(
    dedicated_loop: bool, execution: ExecutionMode
) -> None:
    default = TaskFunction(render, Json(Request), Json(str))
    assert default.execution is ExecutionMode.WORKER
    explicit = TaskFunction(
        render,
        Json(Request),
        Json(str),
        execution=execution,
        dedicated_loop=dedicated_loop,
    )
    assert explicit.execution is ExecutionMode.WORKER


@pytest.mark.parametrize("dedicated_loop", [False, True])
@pytest.mark.parametrize(
    ("execution", "reason"),
    [
        (
            ExecutionMode.OWNGIL,
            "py_task hard-codes mode => worker; "
            "retained main-interpreter closures cannot cross interpreters.*"
            "Task mode support and bodies named by importable module path need "
            "approved erlang-python fork changes.*PyBeam must move task codecs "
            "to the caller and pass raw bytes.*check_multi_interp_extensions reject some extension modules",
        ),
        (
            ExecutionMode.ISOLATED,
            "py_task hard-codes mode => worker.*main-interpreter objects; "
            "fork support.*bootstrap fix.*PR #5",
        ),
        ("owngil", "py_task hard-codes mode => worker.*check_multi_interp_extensions"),
        ("isolated", "py_task hard-codes mode => worker.*bootstrap fix.*PR #5"),
    ],
)
def test_task_function__unavailable_modes(
    execution: ExecutionMode, reason: str, dedicated_loop: bool
) -> None:
    with pytest.raises(CapabilityUnavailable, match=reason):
        TaskFunction(
            render,
            Json(Request),
            Json(str),
            execution=execution,
            dedicated_loop=dedicated_loop,
        )


@pytest.mark.parametrize("execution", ["unknown"])
def test_task_function__unknown_execution_mode(execution: ExecutionMode) -> None:
    with pytest.raises(ValueError, match="'unknown' is not a valid ExecutionMode"):
        TaskFunction(render, Json(Request), Json(str), execution=execution)


def test_task_function__strict_request_and_result_boundaries() -> None:
    function = TaskFunction(render, Json(Request), Json(str))
    assert function.encode(Request(value=21)) == b'{"value":21}'
    assert function.decode(b'"result"') == "result"
    with pytest.raises(CodecError):
        function.decode(b"21")
    with pytest.raises(CodecError):
        Json(TaskLimits).load({"capacity": "1", "max_bytes": 100, "shutdown_ms": 100})


def test_tasks__core_import_does_not_require_native_or_frameworks() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pybeam.tasks; "
            "assert 'erlang' not in sys.modules; assert 'litestar' not in sys.modules; "
            "assert 'fastapi' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_tasks__negative_request_and_result_probes(tmp_path: Path) -> None:
    source = tmp_path / "negative.py"
    source.write_text(
        "from pybeam.codec import Json\n"
        "from pybeam.tasks import TaskFunction, TaskSupervisor\n"
        "async def render(value: int) -> str:\n"
        "    return str(value)\n"
        "async def bad(sup: TaskSupervisor) -> None:\n"
        "    function = TaskFunction(render, Json(int), Json(str))\n"
        "    await sup.start(function, 'wrong')\n"
        "    value: int = await (await sup.start(function, 1))\n"
        "    TaskFunction(render, Json(int), Json(str), execution='worker')\n"
    )
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(root / ".venv/bin/ty"), "check", *TYPECHECK_CONFIG, "--error-on-warning", str(source)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid-argument-type" in result.stdout
    assert "invalid-assignment" in result.stdout
    assert "execution" in result.stdout
