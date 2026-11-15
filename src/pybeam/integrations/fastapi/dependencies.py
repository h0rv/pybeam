from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from starlette.requests import HTTPConnection

from pybeam.integrations.hornbeam import Services, Subscription


async def _connect() -> Services:
    from pybeam.integrations.hornbeam.client import Beam

    return await Beam.connect()


class BeamProvider:
    def __init__(self, connect: Callable[[], Awaitable[Services]] = _connect) -> None:
        self._connect = connect
        self._beam: Services | None = None

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI) -> AsyncGenerator[None]:
        if self._beam is not None:
            message = "BeamProvider lifespan is already running"
            raise RuntimeError(message)
        self._beam = await self._connect()
        try:
            yield
        finally:
            self._beam = None

    async def __call__(self, connection: HTTPConnection) -> Services:
        if self._beam is None:
            message = "BeamProvider requires an active application lifespan"
            raise RuntimeError(message)
        return self._beam.for_scope(connection.scope)

    def subscription(
        self, topic: str, *, max_bytes: int = 65536
    ) -> Callable[[HTTPConnection], AsyncGenerator[Subscription]]:
        async def dependency(connection: HTTPConnection) -> AsyncGenerator[Subscription]:
            beam = await self(connection)
            async with beam.subscribe(topic, max_bytes=max_bytes) as subscription:
                yield subscription

        return dependency


provider = BeamProvider()
type Beam = Annotated[Services, Depends(provider)]
