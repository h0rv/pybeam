import asyncio
from collections.abc import Callable, Coroutine
from typing import Literal

type ServiceState = Literal["running", "completed", "cancelled", "failed"]

class ServiceBusyError(RuntimeError): ...

class ServiceRef:
    def __init__(self, task: asyncio.Task[None]) -> None: ...
    @property
    def state(self) -> ServiceState: ...
    async def wait(self, *, timeout: float | None = None) -> None: ...  # noqa: ASYNC109
    async def stop(self, *, timeout: float = 5.0) -> None: ...  # noqa: ASYNC109

class ServiceSlot:
    def __init__(self, *, capacity: Literal[1]) -> None: ...
    def start[**P](
        self, fn: Callable[P, Coroutine[object, object, None]], *args: P.args, **kwargs: P.kwargs
    ) -> ServiceRef: ...
    async def stop(self, *, timeout: float = 5.0) -> None: ...  # noqa: ASYNC109
