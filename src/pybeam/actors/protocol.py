from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import iscoroutinefunction
from types import FunctionType
from typing import Concatenate, Protocol


class ActorRef[T](Protocol):
    async def call[**P, R](
        self,
        method: Callable[Concatenate[T, P], Awaitable[R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Call an allowed unbound method. Typing does not prove membership.

        The runtime must serialize calls across awaits. Cancellation may abandon
        the reply while the effect continues. Callers must not retry implicitly.
        """
        ...


class MethodError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActorSpec[T]:
    cls: type[T]
    methods: tuple[Callable[..., Awaitable[object]], ...]

    def __post_init__(self) -> None:
        """Register ordinary async instance methods by identity, not by name.

        Inherited methods are allowed unless overridden. Descriptors, bound
        methods, and compatible free functions are not actor operations.
        This registration does not construct an instance or start a process.
        """
        if not self.methods:
            raise MethodError("At least one method is required")
        seen: set[str] = set()
        allowed: set[FunctionType] = set()
        for base in self.cls.__mro__:
            for name, value in vars(base).items():
                if name in seen:
                    continue
                seen.add(name)
                if isinstance(value, FunctionType) and iscoroutinefunction(value):
                    allowed.add(value)
        for method in self.methods:
            if not isinstance(method, FunctionType) or method not in allowed:
                raise MethodError("An operation must be an async instance method of the actor")
        if len(set(self.methods)) != len(self.methods):
            raise MethodError("An operation must not be registered twice")

    def require[**P, R](self, method: Callable[Concatenate[T, P], Awaitable[R]], /) -> None:
        """Reject unregistered callables before runtime dispatch."""
        if not any(method is allowed for allowed in self.methods):
            raise MethodError("The method is not registered for this actor")
