from collections.abc import Callable, Mapping

from pyview.events import InfoEvent
from pyview.live_socket import ConnectedLiveViewSocket, LiveViewSocket

from pybeam.integrations.hornbeam import PresenceEntry, Services
from pybeam.integrations.hornbeam.dependencies import BeamProvider as ServicesProvider
from pybeam.integrations.pyview.subscriptions import ViewPresence, ViewSubscription


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
        return await ViewSubscription.start(
            self._socket_services(socket.websocket.scope),
            socket,
            topic,
            convert,
            max_bytes=max_bytes,
        )

    async def presence[T](
        self,
        socket: ConnectedLiveViewSocket[T],
        topic: str,
        key: str,
        payload: bytes,
        convert: Callable[[tuple[PresenceEntry, ...]], InfoEvent],
        *,
        max_bytes: int = 65536,
    ) -> ViewPresence:
        """Track this view and forward full snapshots using the subscription ownership rules."""
        return await ViewPresence.start(
            self._socket_services(socket.websocket.scope),
            socket,
            topic,
            key,
            payload,
            convert,
            max_bytes=max_bytes,
        )

    def _socket_services(self, scope: Mapping[str, object]) -> Services:
        extensions: object = scope.get("extensions", {})
        if isinstance(extensions, Mapping) and "hornbeam.owner" not in extensions:
            beam = self.services
        else:
            beam = self.for_scope(scope)
        return beam


services = BeamProvider()
