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


def owngil_program(tmp_path: Path, source: str) -> None:
    for name in ("owngil_entries", "owngil_bad_import"):
        fixture = Path(__file__).parent / "native" / (name + ".py")
        (tmp_path / (name + ".py")).write_text(fixture.read_text())
    task_program(tmp_path, "from pybeam.tasks import ExecutionMode\n" + textwrap.dedent(source))


def test_tasks__owngil_caller_codecs_and_entry_script_module_path(tmp_path: Path) -> None:
    owngil_program(
        tmp_path,
        """
        from dataclasses import dataclass
        from pybeam.codec import CodecError
        from owngil_entries import double, echo
        @dataclass(frozen=True, slots=True)
        class Reply:
            value: int
        @dataclass(frozen=True, slots=True)
        class Request:
            value: int
        function = TaskFunction(double, Json(Request), Json(Reply),
                                execution=ExecutionMode.OWNGIL)
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=2000)) as sup:
            ref = await sup.start(function, Request(value=21))
            assert await ref == Reply(value=42)
            try:
                await sup.start(function, Request(value='invalid'))
            except CodecError:
                pass
            else:
                raise AssertionError('The caller accepted an invalid request')
            invalid_result = TaskFunction(echo, Json(str), Json(Reply),
                                          execution=ExecutionMode.OWNGIL)
            ref = await sup.start(invalid_result, 'not a model')
            assert isinstance(await ref.exception(), CodecError)
    """,
    )


def test_tasks__owngil_empty_value_and_empty_result(tmp_path: Path) -> None:
    owngil_program(
        tmp_path,
        """
        from pybeam.codec import CodecError
        from owngil_entries import echo, empty_result
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=2000)) as sup:
            function = TaskFunction(echo, Json(bytes), Json(bytes),
                                    execution=ExecutionMode.OWNGIL)
            assert await (await sup.start(function, b'')) == b''
            function = TaskFunction(empty_result, Json(bytes), Json(bytes),
                                    execution=ExecutionMode.OWNGIL)
            ref = await sup.start(function, b'')
            assert isinstance(await ref.exception(), CodecError)
    """,
    )


def test_tasks__owngil_remote_error(tmp_path: Path) -> None:
    owngil_program(
        tmp_path,
        """
        from erlang.tasks import RemoteTaskError
        from owngil_entries import failure
        function = TaskFunction(failure, Json(int), Json(int),
                                execution=ExecutionMode.OWNGIL)
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=2000)) as sup:
            ref = await sup.start(function, 1)
            error = await ref.exception()
            assert isinstance(error, RemoteTaskError)
            assert error.type_name == 'ValueError' and error.message == 'remote entry failed'
            try:
                await ref.result()
            except RemoteTaskError as raised:
                assert raised is error
            else:
                raise AssertionError('Remote failure was lost')
    """,
    )


def test_tasks__owngil_running_cancel_and_close(tmp_path: Path) -> None:
    owngil_program(
        tmp_path,
        """
        from pathlib import Path
        from owngil_entries import waiting
        function = TaskFunction(waiting, Json(str), Json(type(None)),
                                execution=ExecutionMode.OWNGIL)
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=1000, shutdown_ms=2000)) as sup:
            marker = Path.cwd() / 'entry-running'
            ref = await sup.start(function, str(marker))
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            assert await ref.cancel()
            assert marker.read_text() == 'finished'
            assert isinstance(await ref.exception(), asyncio.CancelledError)
            marker.unlink()
            ref = await sup.start(function, str(marker))
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(0.01)
        assert marker.read_text() == 'finished'
        assert isinstance(await ref.exception(), asyncio.CancelledError)
    """,
    )


def test_tasks__owngil_module_import_failure_is_terminal(tmp_path: Path) -> None:
    owngil_program(
        tmp_path,
        """
        from erlang.tasks import RemoteTaskError
        from owngil_bad_import import echo
        from owngil_entries import double
        function = TaskFunction(echo, Json(int), Json(int),
                                execution=ExecutionMode.OWNGIL)
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=2000)) as sup:
            ref = await sup.start(function, 1)
            error = await ref.exception()
            assert isinstance(error, RemoteTaskError), repr(error)
            assert error.type_name == 'ImportError', str(error)
            assert 'pydantic_core._pydantic_core' in error.message, str(error)
            assert 'does not support loading in subinterpreters' in error.message, str(error)
            healthy = TaskFunction(double, Json(dict[str, int]), Json(dict[str, int]),
                                   execution=ExecutionMode.OWNGIL)
            assert await (await sup.start(healthy, {'value': 2})) == {'value': 4}
    """,
    )


def test_tasks__owngil_cpu_overlap(tmp_path: Path) -> None:
    owngil_program(
        tmp_path,
        """
        import time
        from itertools import combinations
        from owngil_entries import cpu
        function = TaskFunction(cpu, Json(int), Json(tuple[int, int]),
                                execution=ExecutionMode.OWNGIL)
        async with TaskSupervisor(TaskLimits(capacity=3, max_bytes=1000, shutdown_ms=2000)) as sup:
            deadline = time.monotonic_ns() + 3_000_000_000
            refs = [await sup.start(function, deadline) for _ in range(3)]
            spans = await asyncio.gather(*(ref.result() for ref in refs))
            overlaps = [min(left_end, right_end) - max(left_begin, right_begin)
                        for (left_begin, left_end), (right_begin, right_end)
                        in combinations(spans, 2)]
            assert max(overlaps) >= 200_000_000, spans
    """,
    )


def test_tasks__dedicated_uncertainty_keeps_the_existing_watcher_until_cleanup(
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
            body, Json(int), Json(int), dedicated_loop=True
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
        await sup.close()
        assert not sup._refs
        """,
    )


def test_tasks__dedicated_nested_reply_returns_to_its_original_waiter(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        executions = {'parent': 0, 'child': 0}
        loops = []
        async with TaskSupervisor(TaskLimits(capacity=2, max_bytes=100, shutdown_ms=1500)) as sup:
            async def child(value: int) -> int:
                executions['child'] += 1
                return value * 2
            async def parent(value: int) -> int:
                executions['parent'] += 1
                loops.append(asyncio.get_running_loop())
                other = await sup.start(TaskFunction(
                    child, Json(int), Json(int), dedicated_loop=True
                ), value)
                return await other
            ref = await sup.start(TaskFunction(
                parent, Json(int), Json(int), dedicated_loop=True
            ), 21)
            pid = ref.pid
            async with asyncio.timeout(4):
                assert await ref == 42
            waiter = ref._pending
            assert waiter is not None and waiter.get_loop() is asyncio.get_running_loop()
            assert await ref == 42
            assert ref._pending is waiter and ref.pid is pid
            assert executions == {'parent': 1, 'child': 1}
            assert loops[0].is_closed()
            info = await async_call('_task_info', sup._group)
            assert info['jobs'] == 0 and info['contexts'] == [] and info['children'] == []
            assert info['gate']['count'] == info['gate']['bytes'] == info['gate']['unsettled'] == 0
        """,
    )


@pytest.mark.parametrize("dedicated_loop", [False, True])
@pytest.mark.parametrize("observation", ["result", "exception"])
def test_tasks__observed_original_exception_releases_native_ownership(
    tmp_path: Path, observation: str, dedicated_loop: bool
) -> None:
    task_program(
        tmp_path,
        "        observation = "
        + repr(observation)
        + "\n        dedicated_loop = "
        + repr(dedicated_loop)
        + "\n"
        + """
        import gc
        import weakref
        import erlang
        class OriginalError(Exception):
            pass
        baseline = erlang._actor_retained_object_stats()
        async def scenario():
            cause = OriginalError('cause')
            original = OriginalError('original')
            weak = weakref.ref(original)
            cause_weak = weakref.ref(cause)
            tracebacks = []
            async def fail(value: int) -> int:
                try:
                    raise original from cause
                except OriginalError:
                    tracebacks.append(original.__traceback__)
                    raise
            sup = await TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=1000)).__aenter__()
            ref = await sup.start(TaskFunction(
                fail, Json(int), Json(int), dedicated_loop=dedicated_loop
            ), 1)
            async def observe():
                if observation == 'exception':
                    assert await ref.exception() is original
                else:
                    try:
                        await ref.result()
                    except OriginalError as error:
                        assert error is original
                    else:
                        raise AssertionError('original failure lost')
            async with asyncio.TaskGroup() as waiters:
                for _ in range(3):
                    waiters.create_task(observe())
            assert await ref.cancel() is False
            assert await ref.exception() is original
            assert original.__cause__ is cause
            assert original.__suppress_context__ is True
            names = []
            original_traceback_found = False
            traceback = original.__traceback__
            while traceback is not None:
                original_traceback_found |= traceback is tracebacks[0]
                names.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
            assert 'fail' in names
            assert original_traceback_found
            await sup.close()
            await observe()
            assert await ref.cancel() is False
            return weak, cause_weak
        weak, cause_weak = await scenario()
        for _ in range(50):
            gc.collect()
            await asyncio.sleep(.01)
            if weak() is None and cause_weak() is None and erlang._actor_retained_object_stats()[1:] == baseline[1:]:
                break
        assert weak() is None
        assert cause_weak() is None
        stats = erlang._actor_retained_object_stats()
        assert stats[1:] == baseline[1:], (baseline, stats)
        # BEAM may keep one empty result shell until its own heap collection.
        assert stats[0] <= baseline[0] + 1
        """,
    )


def test_tasks__retained_exception_helper_and_reusable_reference_controls(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        import gc
        import weakref
        import erlang
        from _erlang_impl._actor_common import _result
        class OriginalError(Exception):
            pass
        baseline = erlang._actor_retained_object_stats()
        def make():
            error = OriginalError('cycle')
            weak = weakref.ref(error)
            try:
                _result(('error', ('python_error', erlang._actor_retain(error))))
            except OriginalError as raised:
                assert raised is error
            return weak
        weak = make()
        for _ in range(50):
            gc.collect()
            await asyncio.sleep(.01)
            if weak() is None:
                break
        assert weak() is None
        def controls():
            value = {'raw': object()}
            def function():
                return value
            for original in (value, function, OriginalError('reusable')):
                retained = erlang._actor_retain(original)
                assert erlang._actor_unwrap(retained) is original
                assert erlang._actor_unwrap(retained) is original
                if not isinstance(original, BaseException):
                    try:
                        erlang._actor_take_exception(retained)
                    except TypeError:
                        pass
                    else:
                        raise AssertionError('non-exception was consumed')
                    assert erlang._actor_unwrap(retained) is original
            retained = erlang._actor_retain(OriginalError('transferred'))
            error = erlang._actor_take_exception(retained)
            assert type(error) is OriginalError
            for recover in (erlang._actor_take_exception, erlang._actor_unwrap):
                try:
                    recover(retained)
                except (TypeError, RuntimeError):
                    pass
                else:
                    raise AssertionError('consumed reference was reused')
        controls()
        for _ in range(50):
            gc.collect()
            await asyncio.sleep(.01)
            if erlang._actor_retained_object_stats() == baseline:
                break
        assert erlang._actor_retained_object_stats() == baseline
        """,
    )


def test_tasks__direct_self_waits_are_denied_before_watcher_or_cancel(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        from pybeam.tasks import TaskOwnershipError
        release = asyncio.Event()
        denied = asyncio.Event()
        descendant_release = asyncio.Event()
        descendants = []
        refs = []
        async def other(value: int) -> int:
            return value * 2
        async def job(value: int) -> int:
            await release.wait()
            ref = refs[0]
            for wait in (ref.result, ref.exception, ref.cancel):
                try:
                    await wait()
                except TaskOwnershipError:
                    assert ref._pending is None
                else:
                    raise AssertionError('self-dependent wait was allowed')
            denied.set()
            async def descendant():
                await descendant_release.wait()
                return await ref
            descendants.append(asyncio.create_task(descendant()))
            sibling = await sup.start(TaskFunction(other, Json(int), Json(int)), value)
            return await sibling
        async with TaskSupervisor(TaskLimits(capacity=2, max_bytes=100, shutdown_ms=1000)) as sup:
            ref = await sup.start(TaskFunction(job, Json(int), Json(int)), 21)
            refs.append(ref)
            release.set()
            await denied.wait()
            assert await ref == 42
            descendant_release.set()
            assert await descendants[0] == 42
        """,
    )


def test_tasks__pid_barrier_original_error_temporary_exit_and_failure_containment(
    tmp_path: Path,
) -> None:
    task_program(
        tmp_path,
        """
        entered = asyncio.Event()
        release = asyncio.Event()
        original = ValueError("original task error")
        async def good(value: int) -> int:
            entered.set()
            await release.wait()
            return value * 2
        async def bad(value: int) -> int:
            raise original
        async with TaskSupervisor(TaskLimits(capacity=2, max_bytes=4096, shutdown_ms=1000)) as sup:
            good_ref = await sup.start(TaskFunction(good, Json(int), Json(int)), 21)
            await entered.wait()
            assert not release.is_set()
            bad_ref = await sup.start(TaskFunction(bad, Json(int), Json(int)), 0)
            assert bad_ref.pid != good_ref.pid
            info = await async_call('_task_info', sup._group)
            assert good_ref.pid not in info['contexts']
            assert await bad_ref.exception() is original
            try:
                await bad_ref
            except ValueError as error:
                assert error is original
            else:
                raise AssertionError('exception lost')
            release.set()
            assert await good_ref == 42
            assert await good_ref == 42
            info = await async_call('_task_info', sup._group)
            assert info['children'] == []
            assert info['jobs'] == 0
    """,
    )


def test_tasks__waiter_cancellation_does_not_cancel_job_or_release_credit(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        entered = asyncio.Event()
        release = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            await release.wait()
            return value
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            ref = await sup.start(TaskFunction(job, Json(int), Json(int)), 7)
            await entered.wait()
            waiter = asyncio.create_task(ref.result())
            await asyncio.sleep(0)
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                pass
            info = await async_call('_task_info', sup._group)
            assert info['gate']['count'] == 1
            try:
                await sup.start(TaskFunction(job, Json(int), Json(int)), 9)
            except Overloaded:
                pass
            else:
                raise AssertionError('owned slot released prematurely')
            release.set()
            assert await ref == 7
    """,
    )


def test_tasks__cooperative_cancel_ack_waits_for_finally_and_pid_exit(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        cleaned = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish_cleanup.wait()
                cleaned.set()
            return value
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            ref = await sup.start(TaskFunction(job, Json(int), Json(int)), 1)
            await entered.wait()
            cancelling = asyncio.create_task(ref.cancel())
            await cleaning.wait()
            assert not cancelling.done()
            info = await async_call('_task_info', sup._group)
            assert info['gate']['count'] == 1
            finish_cleanup.set()
            assert await cancelling is True
            assert cleaned.is_set()
            try:
                await ref
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError('cancelled task returned success')
            info = await async_call('_task_info', sup._group)
            assert info['children'] == []
    """,
    )


def test_tasks__body_and_python_descendant_cannot_close_their_owner(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        from pybeam.tasks import TaskOwnershipError
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            async def job(descendant: bool) -> int:
                try:
                    if descendant:
                        await asyncio.create_task(sup.close())
                    else:
                        await sup.close()
                except TaskOwnershipError as error:
                    assert 'external owner' in str(error)
                    assert sup._close_task is None
                    return 7
                raise AssertionError('owned body closed its supervisor')
            function = TaskFunction(job, Json(bool), Json(int))
            for descendant in (False, True):
                ref = await sup.start(function, descendant)
                assert await ref == 7
                info = await async_call('_task_info', sup._group)
                assert info['gate']['closed'] is False
                assert info['children'] == []
        assert sup._group is None
    """,
    )


def test_tasks__native_descendant_cannot_close_ancestor_and_external_nested_owner_can_close(
    tmp_path: Path,
) -> None:
    task_program(
        tmp_path,
        """
        from pybeam.tasks import TaskOwnershipError
        limits = TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=2000)
        async with TaskSupervisor(limits) as outer:
            async def parent(value: int) -> int:
                async with TaskSupervisor(limits) as inner:
                    async def child(request: int) -> int:
                        try:
                            await outer.close()
                        except TaskOwnershipError:
                            assert outer._close_task is None
                            return request
                        raise AssertionError('descendant closed its ancestor')
                    ref = await inner.start(TaskFunction(child, Json(int), Json(int)), value)
                    return await ref
            ref = await outer.start(TaskFunction(parent, Json(int), Json(int)), 7)
            assert await ref == 7
        assert outer._group is None
    """,
    )


def test_tasks__external_close_does_not_cycle_when_body_finally_attempts_self_close(
    tmp_path: Path,
) -> None:
    task_program(
        tmp_path,
        """
        from pybeam.tasks import TaskOwnershipError
        entered = asyncio.Event()
        denied = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                try:
                    await sup.close()
                except TaskOwnershipError:
                    assert sup._close_task is not None
                    denied.set()
            return value
        sup = await TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)).__aenter__()
        await sup.start(TaskFunction(job, Json(int), Json(int)), 1)
        await entered.wait()
        await sup.close()
        assert denied.is_set()
        assert sup._group is None
    """,
    )


def test_tasks__scope_shutdown_cancels_and_awaits_cleanup(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
            return value
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            await sup.start(TaskFunction(job, Json(int), Json(int)), 1)
            await entered.wait()
        assert cleaned.is_set()
    """,
    )


def test_tasks__temporary_slots_are_reusable_after_observed_completion(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        async def job(value: int) -> int:
            return value
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            function = TaskFunction(job, Json(int), Json(int))
            for value in range(20):
                ref = await sup.start(function, value)
                assert await ref == value
                info = await async_call('_task_info', sup._group)
                assert info['children'] == []
                assert info['jobs'] == 0
    """,
    )


def test_tasks__cancel_deadline_is_uncertain_and_does_not_interrupt_finally_twice(
    tmp_path: Path,
) -> None:
    task_program(
        tmp_path,
        """
        from erlang.tasks import UncertainInflight
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        cleaned = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()
            return value
        sup = await TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=300)).__aenter__()
        ref = await sup.start(TaskFunction(job, Json(int), Json(int)), 1)
        await entered.wait()
        try:
            await ref.cancel()
        except UncertainInflight:
            pass
        else:
            raise AssertionError('cancellation timeout claimed success')
        await cleaning.wait()
        info = await async_call('_task_info', sup._group)
        assert info['gate']['count'] == 1
        try:
            await sup.close()
        except UncertainInflight:
            pass
        else:
            raise AssertionError('shutdown interrupted cleanup or claimed success')
        assert not cleaned.is_set()
        release.set()
        try:
            await ref
        except asyncio.CancelledError:
            pass
        assert await ref.cancel() is True
        assert cleaned.is_set()
        await sup.close()
    """,
    )


def test_tasks__native_encoded_request_bytes_are_enforced(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        async def job(value: str) -> str:
            raise AssertionError('oversized request executed')
        async with TaskSupervisor(TaskLimits(capacity=2, max_bytes=2, shutdown_ms=1000)) as sup:
            try:
                await sup.start(TaskFunction(job, Json(str), Json(str)), 'large')
            except RuntimeError:
                pass
            else:
                raise AssertionError('byte bound bypassed')
            info = await async_call('_task_info', sup._group)
            assert info['gate']['count'] == 0
            assert info['gate']['bytes'] == 0
            assert info['children'] == []
    """,
    )


def test_tasks__litestar_response_then_handoff_and_application_shutdown(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        from contextlib import asynccontextmanager
        from litestar import Litestar, get
        from pybeam.integrations.litestar.tasks import background_task
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        refs = []
        messages = []
        supervisor = TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000))
        async def job(value: int) -> int:
            assert any(message['type'] == 'http.response.body' for message in messages)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            return value
        callback = background_task(supervisor, TaskFunction(job, Json(int), Json(int)),
                                   42, refs.append)
        @get('/', background=callback)
        async def route() -> str:
            return 'accepted'
        @asynccontextmanager
        async def lifetime(app: object):
            async with supervisor:
                yield
        app = Litestar(route_handlers=[route], lifespan=[lifetime])
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        scope = {'type': 'http', 'path': '/', 'raw_path': b'/', 'root_path': '',
                 'scheme': 'http', 'method': 'GET', 'query_string': b'', 'headers': [],
                 'client': ('127.0.0.1', 1), 'server': ('localhost', 80), 'http_version': '1.1',
                 'asgi': {'version': '3.0'}}
        async with app.lifespan():
            await app(scope, receive, send)
            assert len(refs) == 1
            await entered.wait()
            assert not cleaned.is_set()
            assert any(message.get('status') == 200 for message in messages)
        assert cleaned.is_set()
    """,
    )


def test_tasks__uncertain_constructor_never_closes_a_live_coroutine(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        from erlang.tasks import UncertainInflight
        from types import CoroutineType
        entered = asyncio.Event()
        injected = asyncio.Event()
        release = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        cleaned = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            try:
                await release.wait()
                return value
            finally:
                cleaning.set()
                await finish_cleanup.wait()
                cleaned.set()
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        def factory(owner, coroutine, **kwargs):
            if isinstance(coroutine, CoroutineType) and coroutine.cr_code.co_name == '_outcome' and not injected.is_set():
                options = dict(kwargs)
                options['eager_start'] = True
                asyncio.Task(coroutine, loop=owner, **options)
                injected.set()
                raise MemoryError('injected after scheduling')
            return asyncio.Task(coroutine, loop=owner, **kwargs)
        sup = await TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)).__aenter__()
        loop.set_task_factory(factory)
        try:
            ref = await sup.start(TaskFunction(job, Json(int), Json(int)), 7)
            await injected.wait()
        finally:
            loop.set_task_factory(previous)
        assert entered.is_set()
        try:
            await ref
        except UncertainInflight:
            pass
        else:
            raise AssertionError('constructor uncertainty was hidden')
        assert not cleaning.is_set()
        info = await async_call('_task_info', sup._group)
        assert info['gate']['count'] == 1
        assert info['gate']['closed'] is True
        release.set()
        await cleaning.wait()
        info = await async_call('_task_info', sup._group)
        assert info['gate']['count'] == 1
        finish_cleanup.set()
        await cleaned.wait()
        await sup.close()
    """,
    )


def test_tasks__cancellation_suppressed_by_job_returns_false_and_preserves_result(
    tmp_path: Path,
) -> None:
    task_program(
        tmp_path,
        """
        entered = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return value
            return 0
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            ref = await sup.start(TaskFunction(job, Json(int), Json(int)), 7)
            await entered.wait()
            assert await ref.cancel() is False
            assert await ref == 7
            info = await async_call('_task_info', sup._group)
            assert info['children'] == []
            assert info['gate']['count'] == 0
    """,
    )


def test_tasks__unobserved_success_remains_available_after_scope_close(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        ended = asyncio.Event()
        async def job(value: int) -> int:
            ended.set()
            return value
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            await sup.start(TaskFunction(job, Json(int), Json(int)), 7)
            await ended.wait()
        assert len(sup.unobserved) == 1
        assert await sup.unobserved[0].result() == 7
    """,
    )


def test_tasks__aggregate_native_bytes_and_reuse_after_completion(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        entered = asyncio.Event()
        release = asyncio.Event()
        async def echo(value: str) -> str:
            entered.set()
            await release.wait()
            return value
        async with TaskSupervisor(TaskLimits(capacity=2, max_bytes=16, shutdown_ms=1000)) as sup:
            function = TaskFunction(echo, Json(str), Json(str))
            first = await sup.start(function, 'abcdefghij')
            await entered.wait()
            try:
                await sup.start(function, '12345')
            except Overloaded:
                pass
            else:
                raise AssertionError('aggregate native bytes exceeded')
            info = await async_call('_task_info', sup._group)
            assert info['gate']['count'] == 1
            assert info['gate']['bytes'] == 12
            release.set()
            assert await first == 'abcdefghij'
            second = await sup.start(function, '12345')
            assert await second == '12345'
    """,
    )


def test_tasks__request_is_decoded_data_not_the_original_object_graph(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        from dataclasses import dataclass
        @dataclass(kw_only=True)
        class Request:
            value: int
        original = Request(value=21)
        entered = asyncio.Event()
        release = asyncio.Event()
        async def job(request: Request) -> int:
            assert request is not original
            entered.set()
            await release.wait()
            return request.value
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            ref = await sup.start(TaskFunction(job, Json(Request), Json(int)), original)
            await entered.wait()
            original.value = 99
            release.set()
            assert await ref == 21
    """,
    )


def test_tasks__gather_and_unobserved_original_error_on_scope_close(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        async def double(value: int) -> int:
            return value * 2
        async with TaskSupervisor(TaskLimits(capacity=2, max_bytes=4096, shutdown_ms=1000)) as sup:
            function = TaskFunction(double, Json(int), Json(int))
            first = await sup.start(function, 2)
            second = await sup.start(function, 3)
            assert await asyncio.gather(first, second) == [4, 6]
        original = ValueError('unobserved original')
        ended = asyncio.Event()
        async def fail(value: int) -> int:
            ended.set()
            raise original
        try:
            async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
                await sup.start(TaskFunction(fail, Json(int), Json(int)), 0)
                await ended.wait()
        except ExceptionGroup as errors:
            assert errors.exceptions == (original,)
            assert await sup.unobserved[0].exception() is original
        else:
            raise AssertionError('unobserved exception was silently dropped')
    """,
    )


def test_tasks__cancelling_close_waiter_still_finishes_resource_cleanup(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        cleaned = asyncio.Event()
        async def job(value: int) -> int:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()
            return value
        sup = await TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)).__aenter__()
        await sup.start(TaskFunction(job, Json(int), Json(int)), 1)
        await entered.wait()
        closing = asyncio.create_task(sup.close())
        await cleaning.wait()
        closing.cancel()
        assert not closing.done()
        release.set()
        try:
            await closing
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('close waiter cancellation was lost')
        assert cleaned.is_set()
        assert sup._group is None
    """,
    )


def test_tasks__native_channel_worker_cancel_ack_drains_waiter_and_allows_replacement(
    tmp_path: Path,
) -> None:
    task_program(
        tmp_path,
        """
        from erlang.channel import Channel
        channel = Channel.create_bytes(capacity=1, max_bytes=32)
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        finish_cleanup = asyncio.Event()
        cleaned = asyncio.Event()
        async def worker(value: int) -> int:
            entered.set()
            try:
                await channel.receive_bytes()
                return value
            finally:
                cleaning.set()
                await finish_cleanup.wait()
                channel.abort()
                cleaned.set()
        async def replacement(value: int) -> int:
            return value * 2
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            ref = await sup.start(TaskFunction(worker, Json(int), Json(int)), 7)
            await entered.wait()
            assert channel.snapshot()[-1] == 1
            cancelling = asyncio.create_task(ref.cancel())
            await cleaning.wait()
            assert not cancelling.done()
            assert channel.snapshot()[-1] == 0
            info = await async_call('_task_info', sup._group)
            assert info['gate']['count'] == 1
            finish_cleanup.set()
            assert await cancelling is True
            assert cleaned.is_set()
            assert channel.snapshot() == (0, 0, 1, 32, True, 0)
            fresh = await sup.start(TaskFunction(replacement, Json(int), Json(int)), 21)
            assert await fresh == 42
            assert isinstance(await ref.exception(), asyncio.CancelledError)
    """,
    )


def test_tasks__litestar_after_response_handoff_returns_before_effect(tmp_path: Path) -> None:
    task_program(
        tmp_path,
        """
        from pybeam.integrations.litestar.tasks import background_task
        entered = asyncio.Event()
        release = asyncio.Event()
        refs = []
        async def job(value: int) -> int:
            entered.set()
            await release.wait()
            return value
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)) as sup:
            callback = background_task(sup, TaskFunction(job, Json(int), Json(int)),
                                       42, refs.append)
            await callback()
            assert len(refs) == 1
            await entered.wait()
            assert not release.is_set()
            release.set()
            assert await refs[0] == 42
    """,
    )
