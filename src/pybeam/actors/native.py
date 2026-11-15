from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import TYPE_CHECKING, Concatenate, Self

from pybeam.actors.protocol import ActorSpec
from pybeam.mailbox import UnboundedMailbox

if TYPE_CHECKING:
    from erlang.actors import OwnedActor


class OwnedActorRef[T]:
    def __init__(self, spec: ActorSpec[T], native: OwnedActor) -> None:
        self._spec = spec
        self._native = native

    async def call[**P, R](
        self,
        method: Callable[Concatenate[T, P], Awaitable[R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Invoke once. Caller cancellation abandons the reply, not the effect."""
        self._spec.require(method)
        index = next(i for i, allowed in enumerate(self._spec.methods) if method is allowed)
        return await self._native.call(index, method, *args, **kwargs)

    async def stop(self) -> None:
        await self._native.stop()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()


async def spawn[T](spec: ActorSpec[T], *, mailbox: UnboundedMailbox) -> OwnedActorRef[T]:
    """Start an owned native prototype. No bounded default is available yet."""
    if not isinstance(mailbox, UnboundedMailbox):
        raise NotImplementedError("Finite native actor mailbox admission is not implemented")
    from erlang.actors import spawn as native_spawn

    return OwnedActorRef(spec, await native_spawn(spec.cls, spec.methods, mailbox="unbounded"))
