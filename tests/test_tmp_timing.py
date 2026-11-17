import textwrap
from pathlib import Path

import pytest

from tests.native.task_helpers import run_native

pytestmark = pytest.mark.runtime


@pytest.fixture(autouse=True)
def require_runtime(request: pytest.FixtureRequest) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires explicit --run-runtime and the paired Task-enabled runtime")


def task_program(tmp_path: Path, source: str) -> None:
    body = "    from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor\n"
    body += "    from erlang import async_call\n"
    body += textwrap.indent(textwrap.dedent(source), "    ")
    result = run_native(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "stealing control" not in result.stdout + result.stderr
    assert "native-submission-ok" in result.stdout
    print(result.stdout)


def test_timing(
    tmp_path: Path,
) -> None:
    task_program(
        tmp_path,
        """
        import threading
        import erlang
        from erlang.tasks import UncertainInflight
        entered = threading.Event()
        cleaning = threading.Event()
        finish = threading.Event()
        cleaned = threading.Event()
        executions = []
        async def wait_for(event):
            async with asyncio.timeout(3):
                while not event.is_set():
                    await asyncio.sleep(.001)
        async def body(value: int) -> int:
            executions.append(value)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await wait_for(finish)
                cleaned.set()
            return value
        sup = await TaskSupervisor(TaskLimits(
            capacity=1, max_bytes=100, shutdown_ms=200
        )).__aenter__()
        ref = await sup.start(TaskFunction(
            body, TypeAdapter(int), TypeAdapter(int), dedicated_loop=True
        ), 42)
        await wait_for(entered)
        try:
            acknowledged = await ref.cancel()
        except UncertainInflight:
            assert len(sup._refs) == 1
            assert not cleaned.is_set()
        else:
            assert acknowledged is True and cleaned.is_set()
        await wait_for(cleaning)
        watcher = ref._pending
        assert watcher is not None
        assert not watcher.done() and not cleaned.is_set()
        info = await async_call('_task_info', sup._group)
        assert info['jobs'] == 1 and len(info['contexts']) == 1
        assert info['gate']['count'] == 1
        finish.set()
        # Observe only the existing reply watcher. Do not cancel or submit again.
        async with asyncio.timeout(4):
            await asyncio.shield(watcher)
        assert ref._pending is watcher
        assert isinstance(await ref.exception(), asyncio.CancelledError)
        assert cleaned.is_set() and executions == [42]
        t0 = asyncio.get_running_loop().time()
        tries = 0
        while True:
            try:
                await sup.close()
                break
            except UncertainInflight as e:
                tries += 1
                print("RETRY", e)
        print("CLOSE_MS", (asyncio.get_running_loop().time()-t0)*1000, tries)
        assert not sup._refs
        """,
    )

