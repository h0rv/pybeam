from collections.abc import AsyncGenerator, Callable
from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from pybeam.integrations.hornbeam import Services, Subscription
from pybeam.integrations.hornbeam.dependencies import BeamProvider as ServicesProvider


class BeamProvider(ServicesProvider):
    async def __call__(self, connection: HTTPConnection) -> Services:
        return self.services.for_scope(connection.scope)

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
