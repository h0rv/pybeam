from collections.abc import Callable, Mapping

from pyview.events import InfoEvent
from pyview.live_socket import ConnectedLiveViewSocket, LiveViewSocket

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.hornbeam.dependencies import BeamProvider as ServicesProvider
from pybeam.integrations.pyview.subscriptions import ViewSubscription


class BeamProvider(ServicesProvider):
    """Inject the lifespan connection and open subscriptions for connected views."""

    def __call__[T](self, socket: LiveViewSocket[T]) -> Services:
        """Return the shared lifespan connection without changing its owner."""
        return self.services

    async def subscribe[T](
        self,
        socket: ConnectedLiveViewSocket[T],
        topic: str,
        convert: Callable[[bytes], InfoEvent],
        *,
        max_bytes: int = 65536,
    ) -> ViewSubscription:
        """Use the socket's native owner when present, otherwise the lifespan owner."""
        scope = socket.websocket.scope
        extensions: object = scope.get("extensions", {})
        if isinstance(extensions, Mapping) and "hornbeam.owner" not in extensions:
            beam = self.services
        else:
            beam = self.for_scope(scope)
        return await ViewSubscription.start(beam, socket, topic, convert, max_bytes=max_bytes)


services = BeamProvider()
