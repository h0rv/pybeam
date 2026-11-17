from urllib.parse import ParseResult

from pyview.binding.params import Params
from pyview.live_socket import LiveViewSocket

class BindContext[T]:
    def __init__(
        self,
        params: Params,
        payload: dict[str, object] | None,
        url: ParseResult | None,
        socket: LiveViewSocket[T] | None,
        event: str | None,
    ) -> None: ...
