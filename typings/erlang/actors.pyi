from collections.abc import Awaitable, Callable
from typing import Concatenate, Literal

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
