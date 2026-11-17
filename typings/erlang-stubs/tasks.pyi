from collections.abc import Awaitable, Callable
from typing import Literal, final

type Outcome = tuple[Literal[True], bytes] | tuple[Literal[False], BaseException]
type EntryResult = (
    tuple[Literal["ok"], bytes]
    | tuple[Literal["cancelled"]]
    | tuple[Literal["remote_error"], str, str]
)

@final
class EntryPoint:
    module: str
    name: str
    def __init__(self, function: Callable[[bytes], Awaitable[bytes]]) -> None: ...
    async def __call__(self, token: int, payload: bytes) -> EntryResult: ...

def entrypoint(function: Callable[[bytes], Awaitable[bytes]]) -> EntryPoint: ...

class RemoteTaskError(RuntimeError):
    type_name: str
    message: str
    def __init__(self, type_name: str, message: str) -> None: ...

@final
class Body:
    def __init__(
        self, invoke: Callable[[bytes], Awaitable[bytes]], *, dedicated_loop: bool = False
    ) -> None: ...

async def open_group(count: int, max_bytes: int) -> tuple[object, object]: ...
async def start(
    group: object,
    gate: object,
    factory: Callable[[], Body] | EntryPoint,
    payload: bytes,
    *,
    mode: Literal["worker", "owngil"] = "worker",
) -> object: ...
async def outcome(group: object, pid: object) -> Outcome: ...
async def cancel(group: object, pid: object) -> None: ...
async def shutdown(group: object, milliseconds: int) -> None: ...
async def dispose(group: object) -> None: ...

class UncertainInflight(RuntimeError): ...
