import textwrap
from pathlib import Path

import pytest

from tests.test_submissions_native import run_native

pytestmark = pytest.mark.runtime


def task_program(tmp_path: Path, source: str) -> None:
    body = "    from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor\n"
    body += "    from erlang import async_call\n"
    body += textwrap.indent(textwrap.dedent(source), "    ")
    result = run_native(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native-submission-ok" in result.stdout


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
            good_ref = await sup.start(TaskFunction(good, TypeAdapter(int), TypeAdapter(int)), 21)
            await entered.wait()
            assert not release.is_set()
            bad_ref = await sup.start(TaskFunction(bad, TypeAdapter(int), TypeAdapter(int)), 0)
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
            ref = await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 7)
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
                await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 9)
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
            ref = await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 1)
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
            function = TaskFunction(job, TypeAdapter(bool), TypeAdapter(int))
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
                    ref = await inner.start(TaskFunction(child, TypeAdapter(int), TypeAdapter(int)), value)
                    return await ref
            ref = await outer.start(TaskFunction(parent, TypeAdapter(int), TypeAdapter(int)), 7)
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
        await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 1)
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
            await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 1)
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
            function = TaskFunction(job, TypeAdapter(int), TypeAdapter(int))
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
        ref = await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 1)
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
                await sup.start(TaskFunction(job, TypeAdapter(str), TypeAdapter(str)), 'large')
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
        callback = background_task(supervisor, TaskFunction(job, TypeAdapter(int), TypeAdapter(int)),
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
            ref = await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 7)
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
            ref = await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 7)
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
            await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 7)
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
            function = TaskFunction(echo, TypeAdapter(str), TypeAdapter(str))
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
        from pydantic import BaseModel, ConfigDict
        class Request(BaseModel):
            model_config = ConfigDict(strict=True, extra='forbid')
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
            ref = await sup.start(TaskFunction(job, TypeAdapter(Request), TypeAdapter(int)), original)
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
            function = TaskFunction(double, TypeAdapter(int), TypeAdapter(int))
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
                await sup.start(TaskFunction(fail, TypeAdapter(int), TypeAdapter(int)), 0)
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
        await sup.start(TaskFunction(job, TypeAdapter(int), TypeAdapter(int)), 1)
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
            ref = await sup.start(TaskFunction(worker, TypeAdapter(int), TypeAdapter(int)), 7)
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
            fresh = await sup.start(TaskFunction(replacement, TypeAdapter(int), TypeAdapter(int)), 21)
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
            callback = background_task(sup, TaskFunction(job, TypeAdapter(int), TypeAdapter(int)),
                                       42, refs.append)
            await callback()
            assert len(refs) == 1
            await entered.wait()
            assert not release.is_set()
            release.set()
            assert await refs[0] == 42
    """,
    )
