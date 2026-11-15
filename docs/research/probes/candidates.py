from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Concatenate, Protocol


class Counter(ABC):
    @abstractmethod
    async def add(self, amount: int, /, *, label: str) -> int:
        raise NotImplementedError


class Other(ABC):
    @abstractmethod
    async def read(self, key: bytes, /) -> bytes:
        raise NotImplementedError


class ActorRef[T](Protocol):
    async def call[**P, R](
        self,
        method: Callable[Concatenate[T, P], Awaitable[R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R: ...


class CounterRef(Protocol):
    async def add(self, amount: int, /, *, label: str) -> int: ...
