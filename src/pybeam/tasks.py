import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Protocol, Self, assert_never

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pybeam.submissions import AdmissionClosed, CapabilityUnavailable, Overloaded

if TYPE_CHECKING:
    from erlang.tasks import Body


_body_owners: ContextVar[tuple[object, ...]] = ContextVar("pybeam_task_body_owners", default=())


class TaskOwnershipError(RuntimeError):
    pass


class TaskLimits(BaseModel):
    """Finite ownership slots and aggregate encoded request bytes, not a Python heap limit."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    capacity: Annotated[int, Field(gt=0, le=18446744073709551615)]
    max_bytes: Annotated[int, Field(gt=0, le=18446744073709551615)]
    shutdown_ms: Annotated[int, Field(gt=0, le=4294967295)]


class TaskFunction[Q, R]:
    """A typed request and result codec. Budgets cover encoded requests, not closures."""

    def __init__(
        self,
        function: Callable[[Q], Awaitable[R]],
        request: TypeAdapter[Q],
        result: TypeAdapter[R],
    ) -> None:
        self._function = function
        self._request = request
        self._result = result

    def encode(self, request: Q) -> bytes:
        return self._request.dump_json(self._request.validate_python(request, strict=True))

    def decode(self, payload: bytes) -> R:
        return self._result.validate_json(payload, strict=True)

    def body(self, owners: tuple[object, ...]) -> Body:
        from erlang.tasks import Body

        async def invoke(payload: bytes) -> bytes:
            token = _body_owners.set(owners)
            try:
                request = self._request.validate_json(payload, strict=True)
                value = await self._function(request)
                return self._result.dump_json(self._result.validate_python(value, strict=True))
            finally:
                _body_owners.reset(token)

        return Body(invoke)


@dataclass(frozen=True, slots=True)
class _Success[T]:
    value: T


@dataclass(frozen=True, slots=True)
class _Failure:
    error: BaseException


async def _owned[T](pending: asyncio.Task[T]) -> T:
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
    value = pending.result()
    if cancelled:
        raise asyncio.CancelledError
    return value


class UnobservedTask(Protocol):
    @property
    def pid(self) -> object: ...
    async def result(self) -> object: ...
    async def exception(self) -> BaseException | None: ...


class _OwnedTask(UnobservedTask, Protocol):
    async def _failure(self) -> BaseException | None: ...


class TaskRef[T]:
    def __init__(
        self,
        group: object,
        pid: object,
        decode: Callable[[bytes], T],
        observed: Callable[[], None],
        shutdown_ms: int,
    ) -> None:
        self.pid = pid
        self._shutdown_ms = shutdown_ms
        self._group = group
        self._decode = decode
        self._observed = observed
        self._pending: asyncio.Task[_Success[T] | _Failure] | None = None
        self.observed = False

    async def _receive(self) -> _Success[T] | _Failure:
        from erlang.tasks import UncertainInflight, outcome

        try:
            match await outcome(self._group, self.pid):
                case True, value:
                    return _Success(self._decode(value))
                case False, error:
                    return _Failure(error)
                case unexpected:
                    assert_never(unexpected)
        except asyncio.CancelledError:
            return _Failure(UncertainInflight("The reply watcher was cancelled"))
        except BaseException as error:
            return _Failure(error)

    async def _outcome(self) -> _Success[T] | _Failure:
        if self._pending is None:
            self._pending = asyncio.create_task(self._receive())
        return await asyncio.shield(self._pending)

    async def _failure(self) -> BaseException | None:
        outcome = await self._outcome()
        return outcome.error if isinstance(outcome, _Failure) else None

    async def exception(self) -> BaseException | None:
        """Observe the original failure, or uncertainty if execution cannot be confirmed."""
        error = await self._failure()
        self.observed = True
        self._observed()
        return error

    async def result(self) -> T:
        """Wait without cancelling the job when this waiter is cancelled."""
        outcome = await self._outcome()
        self.observed = True
        self._observed()
        match outcome:
            case _Success(value):
                return value
            case _Failure(error):
                raise error
            case unexpected:
                assert_never(unexpected)

    def __await__(self) -> Generator[object, None, T]:
        return self.result().__await__()

    async def cancel(self) -> bool:
        """Request cooperative cancellation and await actual Python cleanup and PID exit.

        A job may suppress cancellation and return normally. A timeout or killed
        coordinator is uncertainty, never a successful cancellation acknowledgement.
        """
        from erlang.tasks import UncertainInflight, cancel

        try:
            async with asyncio.timeout(self._shutdown_ms / 1000):
                if self._pending is None or not self._pending.done():
                    await cancel(self._group, self.pid)
                outcome = await self._outcome()
        except TimeoutError as error:
            raise UncertainInflight(
                "Task cancellation did not complete before the deadline"
            ) from error
        if isinstance(outcome, _Failure) and isinstance(outcome.error, UncertainInflight):
            raise outcome.error
        cancelled = isinstance(outcome, _Failure) and isinstance(
            outcome.error, asyncio.CancelledError
        )
        if cancelled:
            self.observed = True
            self._observed()
        return cancelled


class TaskSupervisor:
    """Application-owned temporary OTP task children. Failure does not cancel siblings.

    Completed unobserved tasks occupy slots until awaited or scope shutdown.
    Shutdown retains their handles in unobserved and reports unobserved errors.
    Only an external owner may close the supervisor, not its bodies or descendants.
    This is not Python TaskGroup and never retries effects or restarts a job.
    Cancellation cannot undo completed effects or interrupt a blocked C body.
    A native VM crash is not isolated to a task.
    """

    def __init__(self, limits: TaskLimits) -> None:
        self._limits = limits
        self._group: object | None = None
        self._gate: object | None = None
        self._refs: dict[object, _OwnedTask] = {}
        self.unobserved: tuple[UnobservedTask, ...] = ()
        self._closing = False
        self._starting = 0
        self._close_task: asyncio.Task[None] | None = None
        self._shutdown_rpc: asyncio.Task[None] | None = None
        self._dispose_rpc: asyncio.Task[None] | None = None
        self._unobserved_errors: tuple[BaseException, ...] = ()
        self._admissions_settled = asyncio.Event()
        self._admissions_settled.set()

    async def __aenter__(self) -> Self:
        try:
            from erlang.tasks import open_group
        except ImportError as error:
            raise CapabilityUnavailable("A task-enabled native backend is required") from error

        if self._group is not None or self._closing:
            raise RuntimeError("Task supervisor cannot be reused")
        pending = asyncio.create_task(open_group(self._limits.capacity, self._limits.max_bytes))
        try:
            self._group, self._gate = await _owned(pending)
        except asyncio.CancelledError:
            self._group, self._gate = pending.result()
            await self.close()
            raise
        return self

    async def start[Q, R](self, function: TaskFunction[Q, R], request: Q) -> TaskRef[R]:
        from erlang.actors import ActorOverloaded
        from erlang.tasks import start

        if self._group is None or self._gate is None or self._closing:
            raise AdmissionClosed("Task supervisor is not open")
        if len(self._refs) + self._starting >= self._limits.capacity:
            raise Overloaded("Task ownership slots are full, including unobserved results")
        group = self._group
        owners = (*_body_owners.get(), group)

        def factory() -> Body:
            return function.body(owners)

        payload = function.encode(request)
        self._starting += 1
        self._admissions_settled.clear()
        cancelled = False
        try:
            pending = asyncio.create_task(start(group, self._gate, factory, payload))
            try:
                pid = await _owned(pending)
            except asyncio.CancelledError:
                pid = pending.result()
                cancelled = True
            except ActorOverloaded as error:
                raise Overloaded("Native task count or encoded request bytes exhausted") from error
        finally:
            self._starting -= 1
            if self._starting == 0:
                self._admissions_settled.set()

        def observed() -> None:
            self._refs.pop(pid, None)

        ref = TaskRef(group, pid, function.decode, observed, self._limits.shutdown_ms)
        self._refs[pid] = ref
        if cancelled:
            await ref.cancel()
            raise asyncio.CancelledError
        return ref

    async def close(self) -> None:
        if self._group is not None and self._group in _body_owners.get():
            raise TaskOwnershipError(
                "A supervised body or descendant cannot close its own TaskSupervisor; "
                "ask the external owner to close it"
            )
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        try:
            await _owned(self._close_task)
        except BaseException:
            self._close_task = None
            raise

    async def _close(self) -> None:
        from erlang.tasks import UncertainInflight, dispose, shutdown

        if self._group is None:
            return
        self._closing = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._limits.shutdown_ms / 1000
        if self._dispose_rpc is not None:
            await self._finish_close(deadline)
            return
        try:
            async with asyncio.timeout_at(deadline):
                await self._admissions_settled.wait()
        except TimeoutError as error:
            raise UncertainInflight("Task admission has not settled") from error
        # Retain a late native shutdown reply across retries. No timeout can
        # abandon ownership, replay work, or release its native reservation.
        if self._shutdown_rpc is not None and self._shutdown_rpc.done():
            try:
                self._shutdown_rpc.result()
            except UncertainInflight:
                self._shutdown_rpc = None
        if self._shutdown_rpc is None:
            remaining_ms = max(1, int((deadline - loop.time()) * 1000))
            self._shutdown_rpc = asyncio.create_task(shutdown(self._group, remaining_ms))
        done, _ = await asyncio.wait(
            (self._shutdown_rpc,), timeout=max(0.0, deadline - loop.time())
        )
        if not done:
            raise UncertainInflight("Task shutdown has not acknowledged completion")
        try:
            self._shutdown_rpc.result()
        finally:
            self._shutdown_rpc = None
        failures: list[BaseException] = []
        try:
            async with asyncio.timeout_at(deadline):
                for ref in tuple(self._refs.values()):
                    error = await ref._failure()
                    if error is not None and not isinstance(error, asyncio.CancelledError):
                        failures.append(error)
        except TimeoutError as error:
            raise UncertainInflight("Completed task replies have not settled") from error
        self._unobserved_errors = tuple(failures)
        self._dispose_rpc = asyncio.create_task(dispose(self._group))
        await self._finish_close(deadline)

    async def _finish_close(self, deadline: float) -> None:
        from erlang.tasks import UncertainInflight

        pending = self._dispose_rpc
        if pending is None:
            raise RuntimeError("No native disposal is pending")
        done, _ = await asyncio.wait(
            (pending,), timeout=max(0.0, deadline - asyncio.get_running_loop().time())
        )
        if not done:
            raise UncertainInflight("Supervisor resource cleanup has not acknowledged completion")
        pending.result()
        self._group = None
        self._gate = None
        self.unobserved = tuple(self._refs.values())
        self._refs.clear()
        failures = self._unobserved_errors
        self._unobserved_errors = ()
        if failures:
            raise BaseExceptionGroup("Unobserved supervised task failures", failures)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()
