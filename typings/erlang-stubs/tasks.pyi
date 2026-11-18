from collections.abc import Awaitable, Callable
from typing import Literal, final

type Outcome = tuple[Literal[True], bytes] | tuple[Literal[False], BaseException]
type EntryResult = (
    tuple[Literal["ok"], bytes]
    | tuple[Literal["cancelled"]]
    | tuple[Literal["remote_error"], str, str]
)

class RemoteTaskError(RuntimeError):
    type_name: str
    message: str
    def __init__(self, type_name: str, message: str) -> None: ...

class TaskCrashed(RuntimeError):
    reason: object
    def __init__(self, reason: object) -> None: ...

@final
class Body:
    def __init__(
        self, invoke: Callable[[bytes], Awaitable[bytes]], *, dedicated_loop: bool = False
    ) -> None: ...

async def open_group(count: int, max_bytes: int) -> tuple[object, object]: ...
async def start(
    group: object,
    gate: object,
    factory: Callable[[], Body] | Callable[[bytes], Awaitable[bytes]],
    payload: bytes,
    *,
    mode: Literal["worker", "owngil", "isolated"] = "worker",
) -> object: ...
async def outcome(group: object, pid: object) -> Outcome: ...
async def cancel(group: object, pid: object) -> None: ...
async def thread_snapshot(group: object, pid: object) -> tuple[int, int, bytes]: ...
async def terminate(group: object, pid: object) -> None: ...
async def shutdown(group: object, milliseconds: int) -> None: ...
async def dispose(group: object) -> None: ...

class UncertainInflight(RuntimeError): ...
