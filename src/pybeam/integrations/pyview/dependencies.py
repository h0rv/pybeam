from collections.abc import Callable

from pyview.events import InfoEvent
from pyview.live_socket import ConnectedLiveViewSocket, LiveViewSocket

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.hornbeam.dependencies import BeamProvider as ServicesProvider
from pybeam.integrations.pyview.subscriptions import ViewSubscription


class BeamProvider(ServicesProvider):
    """Inject the lifespan connection and scope subscriptions to connected sockets."""

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
        """Start a socket-owned subscription; unowned scopes raise."""
        beam = self.for_scope(socket.websocket.scope)
        return await ViewSubscription.start(beam, socket, topic, convert, max_bytes=max_bytes)


services = BeamProvider()
