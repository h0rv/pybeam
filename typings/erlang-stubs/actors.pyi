from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Concatenate, Literal, Protocol, Self

class DataDispatcher(Protocol):
    async def dispatch(self, payload: bytes, /) -> bytes: ...

class ActorOverloaded(RuntimeError): ...
class ActorAdmissionClosed(RuntimeError): ...
class ActorRestarting(RuntimeError): ...

@dataclass(frozen=True, slots=True)
class ActorInfo:
    pid: str
    generation: int
    admitted_count: int
    admitted_bytes: int

@dataclass(frozen=True, slots=True)
class ContextCounts:
    created: int
    destroyed: int
    @property
    def live(self) -> int: ...

async def context_counts() -> ContextCounts: ...

class ActorGenerationChanged(RuntimeError):
    expected: int
    current: int

class DataTicket:
    def abort(self) -> None: ...
    async def send(self) -> bytes: ...

class DataActorRef:
    child_id: str
    generation: int
    @property
    def pid(self) -> object: ...
    def reserve(self, payload: bytes, /) -> DataTicket: ...
    async def call(self, payload: bytes, /) -> bytes: ...
    async def rejoin(self) -> DataActorRef: ...
    async def info(self) -> ActorInfo: ...
    async def crash(self) -> None: ...

class DataActorGroup:
    def __init__(self, *, max_children: int = 64) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def spawn(
        self,
        child_id: str,
        factory: Callable[[], DataDispatcher],
        *,
        capacity: int = 64,
        max_bytes: int = 1048576,
        restart: Literal["permanent", "transient", "temporary"] = "permanent",
    ) -> DataActorRef: ...
    async def stop(self, child_id: str, /) -> None: ...
    async def close(self) -> None: ...
    async def __aexit__(self, *exc: object) -> None: ...

class OwnedActor:
    async def call[T, **P, R](
        self,
        index: int,
        method: Callable[Concatenate[T, P], Awaitable[R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R: ...
    async def stop(self) -> None: ...

async def spawn(
    factory: Callable[[], object],
    methods: tuple[Callable[..., Awaitable[object]], ...],
    *,
    mailbox: Literal["unbounded"],
) -> OwnedActor: ...
