from pyview.live_socket import LiveViewSocket, is_connected

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.hornbeam.dependencies import BeamProvider as ServicesProvider


class BeamProvider(ServicesProvider):
    """Share one lifespan connection; connected sockets retain their own ownership scope."""

    def __call__[T](self, socket: LiveViewSocket[T]) -> Services:
        beam = self.services
        if is_connected(socket):
            return beam.for_scope(socket.websocket.scope)
        return beam


services = BeamProvider()
