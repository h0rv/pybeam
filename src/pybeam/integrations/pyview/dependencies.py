from pyview.live_socket import LiveViewSocket, is_connected

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.hornbeam.dependencies import BeamProvider as ServicesProvider


class BeamProvider(ServicesProvider):
    def __call__[T](self, socket: LiveViewSocket[T]) -> Services:
        if is_connected(socket):
            return self.for_scope(socket.websocket.scope)
        return self.services


services = BeamProvider()
