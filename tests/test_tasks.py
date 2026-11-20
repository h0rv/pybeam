import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_type

import pytest

import pybeam
from pybeam.codec import CodecError, Json
from pybeam.tasks import ExecutionMode, TaskFunction, TaskLimits, TaskRef, TaskSupervisor
from tests.typechecking import TYPECHECK_CONFIG


async def byte_entry(payload: bytes) -> bytes:
    return payload


async def test_isolated__rejects_main_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(byte_entry, "__module__", "__main__")
    with pytest.raises(TypeError, match="import the entry from its module"):
        await pybeam.isolated(byte_entry, b"")


async def test_isolated__rejects_nested_entry() -> None:
    async def nested(payload: bytes) -> bytes:
        return payload

    with pytest.raises(TypeError, match="importable top-level"):
        await pybeam.isolated(nested, b"")


async def test_isolated__rejects_sync_entry() -> None:
    def entry(payload: bytes) -> Awaitable[bytes]:
        return byte_entry(payload)

    with pytest.raises(TypeError, match="plain async Python function"):
        await pybeam.isolated(entry, b"")


@pytest.mark.parametrize("argument", ["'text'", "bytearray()", "None"])
def test_isolated__rejects_nonbytes(argument: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, pybeam\n"
            "from tests.test_tasks import byte_entry\n"
            "async def main():\n"
            "    try:\n"
            "        await pybeam.isolated(byte_entry, " + argument + ")\n"
            "    except TypeError as error:\n"
            "        assert str(error) == 'isolated calls require bytes'\n"
            "    else:\n"
            "        raise AssertionError('Accepted a nonbytes argument')\n"
            "asyncio.run(main())\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def entry() -> Callable[[bytes], Awaitable[bytes]]:
    return byte_entry


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


async def checked_owngil(
    supervisor: TaskSupervisor,
    entry: Callable[[bytes], Awaitable[bytes]],
    execution: Literal[ExecutionMode.OWNGIL, ExecutionMode.ISOLATED],
) -> None:
    function = TaskFunction(entry, Json(Request), Json(str), execution=execution)
    assert_type(function, TaskFunction[Request, str])
    assert_type(await supervisor.start(function, Request(value=1)), TaskRef[str])


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


@pytest.mark.parametrize(
    "execution", [ExecutionMode.OWNGIL, "owngil", ExecutionMode.ISOLATED, "isolated"]
)
def test_task_function__owngil_requires_async_function(execution: ExecutionMode) -> None:
    def not_async(payload: bytes) -> Awaitable[bytes]:
        return byte_entry(payload)

    class CallableEntry:
        async def __call__(self, payload: bytes) -> bytes:
            return payload

    for function in (not_async, CallableEntry(), CallableEntry().__call__):
        with pytest.raises(TypeError, match="tasks require a plain async Python function"):
            TaskFunction(function, Json(bytes), Json(bytes), execution=execution)


@pytest.mark.parametrize(
    "execution", [ExecutionMode.OWNGIL, "owngil", ExecutionMode.ISOLATED, "isolated"]
)
def test_task_function__owngil_entry_codecs(
    entry: Callable[[bytes], Awaitable[bytes]],
    execution: Literal[ExecutionMode.OWNGIL, ExecutionMode.ISOLATED],
) -> None:
    function = TaskFunction(entry, Json(Request), Json(str), execution=execution)
    assert function.execution is ExecutionMode(execution)
    assert function.encode(Request(value=3)) == b'{"value":3}'
    assert function.decode(b'"result"') == "result"
    with pytest.raises(CodecError):
        function.decode(b"")


@pytest.mark.parametrize("execution", [ExecutionMode.OWNGIL, ExecutionMode.ISOLATED])
def test_task_function__owngil_rejects_dedicated_loop(
    entry: Callable[[bytes], Awaitable[bytes]],
    execution: Literal[ExecutionMode.OWNGIL, ExecutionMode.ISOLATED],
) -> None:
    with pytest.raises(ValueError, match="dedicated_loop is worker-only"):
        TaskFunction(
            entry,
            Json(Request),
            Json(str),
            execution=execution,
            dedicated_loop=True,
        )


def test_task_function__worker_accepts_byte_function() -> None:
    function = TaskFunction(byte_entry, Json(bytes), Json(bytes))
    assert function.execution is ExecutionMode.WORKER


@pytest.mark.parametrize(
    ("module", "name"), [("", "render"), (None, "render"), ("entries", "outer.<locals>.render")]
)
@pytest.mark.parametrize("execution", [ExecutionMode.OWNGIL, ExecutionMode.ISOLATED])
def test_task_function__owngil_rejects_nonimportable_entry(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    name: str,
    execution: Literal[ExecutionMode.OWNGIL, ExecutionMode.ISOLATED],
) -> None:
    monkeypatch.setattr(byte_entry, "__module__", module)
    monkeypatch.setattr(byte_entry, "__qualname__", name)
    with pytest.raises(TypeError, match="importable top-level functions"):
        TaskFunction(byte_entry, Json(Request), Json(str), execution=execution)


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
