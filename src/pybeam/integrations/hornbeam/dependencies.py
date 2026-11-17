from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from pybeam.integrations.hornbeam import Beam, Services


class BeamProvider:
    def __init__(self, connect: Callable[[], Awaitable[Services]] = Beam.connect) -> None:
        self._connect = connect
        self._beam: Services | None = None

    @property
    def services(self) -> Services:
        if self._beam is None:
            message = "BeamProvider requires an active application lifespan"
            raise RuntimeError(message)
        return self._beam

    @asynccontextmanager
    async def lifespan(self, _app: object) -> AsyncGenerator[None]:
        if self._beam is not None:
            message = "BeamProvider lifespan is already running"
            raise RuntimeError(message)
        self._beam = await self._connect()
        try:
            yield
        finally:
            self._beam = None
