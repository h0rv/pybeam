import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from inspect import iscoroutinefunction
from types import FunctionType, TracebackType
from typing import TYPE_CHECKING, Literal, Protocol, Self, assert_never, overload

from pybeam.codec import Codec
from pybeam.submissions import AdmissionClosed, CapabilityUnavailable, Overloaded

if TYPE_CHECKING:
    from erlang.tasks import Body


@dataclass(frozen=True, slots=True)
class _BodyExecution:
    job: object
    task: asyncio.Task[object] | None


_body_owners: ContextVar[tuple[object, ...]] = ContextVar("pybeam_task_body_owners", default=())
_body_execution: ContextVar[_BodyExecution | None] = ContextVar(
    "pybeam_task_body_execution", default=None
)


class TaskOwnershipError(RuntimeError):
    pass


class TaskCrashed(RuntimeError):
    """The process exited: execution stopped, external effects unknown, never retried."""

    def __init__(self, reason: object) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskLimits:
    """Finite ownership slots and aggregate encoded request bytes, not a Python heap limit."""

    capacity: int
    max_bytes: int
    shutdown_ms: int

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("capacity", self.capacity, 18446744073709551615),
            ("max_bytes", self.max_bytes, 18446744073709551615),
            ("shutdown_ms", self.shutdown_ms, 4294967295),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(name + " must be an integer from 1 to " + str(maximum))


class ExecutionMode(StrEnum):
    """Execution boundaries supported by erlang-python contexts.

    Worker shares the main interpreter: GIL on 3.14, parallel threads on
    3.14t; a native crash kills the VM. Owngil owns an interpreter per context
    and is not a crash boundary. Owngil tasks use importable byte entries and
    caller-side codecs: their modules must avoid incompatible native extensions.
    Isolated uses a child OS process that contains
    os.abort(), costing roughly 24-40 MiB/context and 76 µs per call in probes.
    All three modes support tasks. Isolated tasks never restart or retry after
    losing their child; TaskCrashed reports stopped execution with unknown effects.
    """

    WORKER = "worker"
    OWNGIL = "owngil"
    ISOLATED = "isolated"


class _BytesCodec:
    def encode(self, value: bytes, /) -> bytes:
        if not isinstance(value, bytes):
            raise TypeError("isolated calls require bytes")
        return value

    def decode(self, payload: bytes, /) -> bytes:
        return self.encode(payload)


async def isolated(fn: Callable[[bytes], Awaitable[bytes]], arg: bytes) -> bytes:
    """Run one importable byte entry in a fresh process, with unknown effects on crash.

    Requests are bounded to 1 MiB and supervisor shutdown to 3000 ms. Each call
    copies its bytes across the process boundary and runs once, including on failure.
    Use TaskSupervisor directly for configurable budgets and task handles.
    """
    codec = _BytesCodec()
    function = TaskFunction(fn, codec, codec, execution=ExecutionMode.ISOLATED)
    if isinstance(fn, FunctionType) and fn.__module__ == "__main__":
        raise TypeError(
            "isolated entries must be importable; "
            "import the entry from its module instead of __main__"
        )
    payload = codec.encode(arg)
    async with TaskSupervisor(
        TaskLimits(capacity=1, max_bytes=1024 * 1024, shutdown_ms=3000)
    ) as supervisor:
        return await (await supervisor.start(function, payload))


class TaskFunction[Q, R]:
    """A typed request and result codec. Budgets cover requests, not closures.

    dedicated_loop runs the body on its context-owned native loop instead of
    the shared driver. It still uses the main interpreter, not an isolation VM.
    Construct loop-bound resources inside the body and finalize them before return.

    Owngil and isolated require a plain top-level async byte function. Owngil
    entry modules must avoid incompatible native extensions.
    The caller encodes requests and decodes results; entries receive and return
    bytes, not Q/R objects. Nested PyBEAM supervisors are not supported inside
    named entries. Cancellation is cooperative and CPU/native-blocked entry
    loops delay it.
    """

    @overload
    def __init__(
        self,
        function: Callable[[bytes], Awaitable[bytes]],
        request: Codec[Q],
        result: Codec[R],
        *,
        execution: Literal[ExecutionMode.OWNGIL, ExecutionMode.ISOLATED],
        dedicated_loop: bool = False,
    ) -> None: ...

    @overload
    def __init__(
        self,
        function: Callable[[Q], Awaitable[R]],
        request: Codec[Q],
        result: Codec[R],
        *,
        execution: ExecutionMode = ExecutionMode.WORKER,
        dedicated_loop: bool = False,
    ) -> None: ...

    def __init__(
        self,
        function: Callable[..., Awaitable[R]],
        request: Codec[Q],
        result: Codec[R],
        *,
        execution: ExecutionMode = ExecutionMode.WORKER,
        dedicated_loop: bool = False,
    ) -> None:
        execution = ExecutionMode(execution)
        if execution != ExecutionMode.WORKER and dedicated_loop:
            raise ValueError(
                "dedicated_loop is worker-only; named tasks own their interpreter loop"
            )
        self._function = function
        self._entry: FunctionType | None = None
        if execution != ExecutionMode.WORKER:
            if not isinstance(function, FunctionType) or not iscoroutinefunction(function):
                raise TypeError(execution.value + " tasks require a plain async Python function")
            if (
                not isinstance(function.__module__, str)
                or not function.__qualname__.isidentifier()
                or not all(part.isidentifier() for part in function.__module__.split("."))
            ):
                raise TypeError(execution.value + " entries must be importable top-level functions")
            self._entry = function
        self._execution = execution
        self._dedicated_loop = dedicated_loop
        self._request = request
        self._result = result

    @property
    def execution(self) -> ExecutionMode:
        return self._execution

    def encode(self, request: Q) -> bytes:
        return self._request.encode(request)

    def decode(self, payload: bytes) -> R:
        return self._result.decode(payload)

    def body(self, owners: tuple[object, ...], job: object | None = None) -> Body:
        from erlang.tasks import Body

        function = self._function
        if self._entry is not None:
            raise TypeError(self._execution.value + " entries cannot run as worker bodies")

        async def invoke(payload: bytes) -> bytes:
            token = _body_owners.set(owners)
            job_token = _body_execution.set(_BodyExecution(job, asyncio.current_task()))
            try:
                request = self._request.decode(payload)
                value = await function(request)
                return self._result.encode(value)
            finally:
                _body_execution.reset(job_token)
                _body_owners.reset(token)

        return Body(invoke, dedicated_loop=self._dedicated_loop)


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


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """A bounded diagnostic sample of one isolated child, not BEAM reductions.

    CPU time covers the entire child process, including imports and diagnostics.
    Compare two samples of the same os_pid to measure CPU use over an interval.
    """

    os_pid: int
    cpu_seconds: float
    stacks: str


class TaskRef[T]:
    def __init__(
        self,
        group: object,
        pid: object,
        decode: Callable[[bytes], T],
        observed: Callable[[], None],
        shutdown_ms: int,
        job: object | None = None,
        execution: ExecutionMode = ExecutionMode.WORKER,
    ) -> None:
        self._job = object() if job is None else job
        self._execution = execution
        self.pid = pid
        self._shutdown_ms = shutdown_ms
        self._group = group
        self._decode = decode
        self._observed = observed
        self._pending: asyncio.Task[_Success[T] | _Failure] | None = None
        self.observed = False

    async def _receive(self) -> _Success[T] | _Failure:
        from erlang.tasks import TaskCrashed as NativeTaskCrashed
        from erlang.tasks import UncertainInflight, outcome

        try:
            match await outcome(self._group, self.pid):
                case True, value:
                    return _Success(self._decode(value))
                case False, error:
                    if isinstance(error, NativeTaskCrashed):
                        return _Failure(TaskCrashed(error.reason))
                    return _Failure(error)
                case unexpected:
                    assert_never(unexpected)
        except asyncio.CancelledError:
            return _Failure(UncertainInflight("The reply watcher was cancelled"))
        except BaseException as error:
            return _Failure(error)

    def _check_wait_owner(self) -> None:
        execution = _body_execution.get()
        if (
            execution is not None
            and execution.job is self._job
            and execution.task is asyncio.current_task()
        ):
            raise TaskOwnershipError("A supervised body cannot wait for its own task completion")

    async def _outcome(self) -> _Success[T] | _Failure:
        self._check_wait_owner()
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
        """Wait without cancelling the job when this waiter is cancelled.

        A body cannot directly wait for its own completion.
        """
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

    async def inspect(self) -> TaskSnapshot:
        """Sample an isolated child's Python stacks even when its loop is busy.

        Only isolated tasks support this operation. A native call holding the
        child's GIL can prevent inspection; it cannot prevent terminate().
        Completed tasks no longer have a child to inspect.
        """
        from erlang.tasks import thread_snapshot

        if self._execution != ExecutionMode.ISOLATED:
            raise ValueError("Task inspection requires isolated execution")
        os_pid, cpu_ns, stacks = await thread_snapshot(self._group, self.pid)
        return TaskSnapshot(
            os_pid, cpu_ns / 1_000_000_000, stacks.decode("utf-8", errors="replace")
        )

    async def terminate(self) -> None:
        """Force an isolated child to exit and observe its settled task outcome.

        Does not run Python finalizers, undo side effects, restart, or retry.
        result() retains the original outcome (normally TaskCrashed). Completion
        can win the race with termination. A deadline is uncertainty, not proof
        of exit. Embedded worker and own-GIL tasks cannot be safely terminated.
        """
        from erlang.tasks import UncertainInflight, terminate

        self._check_wait_owner()
        if self._execution != ExecutionMode.ISOLATED:
            raise ValueError("Forced termination requires isolated execution")
        try:
            async with asyncio.timeout(self._shutdown_ms / 1000):
                if self._pending is None or not self._pending.done():
                    await terminate(self._group, self.pid)
                error = await self.exception()
        except TimeoutError as error:
            raise UncertainInflight("Task termination did not confirm child exit") from error
        if isinstance(error, UncertainInflight):
            raise error

    async def cancel(self) -> bool:
        """Request cooperative cancellation and await actual Python cleanup and PID exit.

        A job may suppress cancellation and return normally. A timeout or killed
        coordinator is uncertainty, never a successful cancellation acknowledgement.
        A body cannot directly await its own cancellation acknowledgement.
        """
        from erlang.tasks import UncertainInflight, cancel

        self._check_wait_owner()
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
    Only isolated tasks contain native child crashes; worker/owngil crashes can
    kill the VM. A lost isolated child is never retried.
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
        job = object()

        def factory() -> Body:
            return function.body(owners, job)

        payload = function.encode(request)
        self._starting += 1
        self._admissions_settled.clear()
        cancelled = False
        try:
            if function._entry is None:
                submission = start(group, self._gate, factory, payload)
            else:
                submission = start(
                    group, self._gate, function._entry, payload, mode=function.execution.value
                )
            pending = asyncio.create_task(submission)
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

        ref = TaskRef(
            group,
            pid,
            function.decode,
            observed,
            self._limits.shutdown_ms,
            job,
            execution=function.execution,
        )
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
