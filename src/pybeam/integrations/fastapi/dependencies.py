from collections.abc import AsyncGenerator, Callable
from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from pybeam.integrations.hornbeam import Services, Subscription
from pybeam.integrations.hornbeam.dependencies import BeamProvider as ServicesProvider


class BeamProvider(ServicesProvider):
    async def __call__(self, connection: HTTPConnection) -> Services:
        """Return the shared lifespan connection without changing its owner."""
        return self.services

    def subscription(
        self, topic: str, *, max_bytes: int = 65536
    ) -> Callable[[HTTPConnection], AsyncGenerator[Subscription]]:
        """Open a subscription owned by the connection; unowned scopes raise."""

        async def dependency(connection: HTTPConnection) -> AsyncGenerator[Subscription]:
            beam = self.for_scope(connection.scope)
            async with beam.subscribe(topic, max_bytes=max_bytes) as subscription:
                yield subscription

        return dependency


provider = BeamProvider()
type Beam = Annotated[Services, Depends(provider)]
