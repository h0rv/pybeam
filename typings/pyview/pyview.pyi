from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from pyview.live_view import LiveView
from pyview.template import RootTemplate
from starlette.applications import Starlette
from starlette.routing import BaseRoute

class PyView(Starlette):
    rootTemplate: RootTemplate
    def __init__(
        self,
        *,
        routes: Sequence[BaseRoute] | None = None,
        lifespan: Callable[[Starlette], AbstractAsyncContextManager[None]] | None = None,
    ) -> None: ...
    def add_live_view[T](self, path: str, view: type[LiveView[T]]) -> None: ...
