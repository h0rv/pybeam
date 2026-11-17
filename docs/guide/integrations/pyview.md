# PyView

Keep a shared counter behind a PyView page. Each click updates the counter atomically and renders the result with PyView's template engine.

```python
# pyview_app.py
import asyncio
from dataclasses import dataclass
from string.templatelib import Template
from typing import override

from pyview import Depends
from pyview.live_socket import LiveViewSocket
from pyview.live_view import LiveView, Session
from pyview.meta import PyViewMeta
from pyview.pyview import PyView
from pyview.template.template_view import TemplateView
from uvicorn import Config, Server

from pybeam.integrations.hornbeam import Services, run
from pybeam.integrations.pyview.dependencies import services


@dataclass
class CounterState:
    count: int = 0


class CounterView(TemplateView[CounterState], LiveView[CounterState]):
    @override
    async def mount(
        self,
        socket: LiveViewSocket[CounterState],
        session: Session,
        beam: Services = Depends(services),
    ) -> None:
        count = await beam.incr("visits", 0, namespace="pyview")
        socket.context = CounterState(count)

    async def handle_event(
        self,
        event: str,
        socket: LiveViewSocket[CounterState],
        beam: Services = Depends(services),
    ) -> None:
        if event == "increment":
            count = await beam.incr("visits", namespace="pyview")
            socket.context.count = count

    @override
    def template(self, assigns: CounterState, meta: PyViewMeta) -> Template:
        return t"""<main>
            <p>{assigns.count}</p>
            <button phx-click="increment">Increment</button>
        </main>"""


app = PyView(lifespan=services.lifespan)
app.add_live_view("/", CounterView)


async def main() -> None:
    config = Config(app, host="127.0.0.1", port=8769, loop="none", http="h11")
    server = Server(config)

    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
```

```console
$ uv run pyview_app.py
```

Open [localhost:8769](http://127.0.0.1:8769/) in your browser. The counter starts at `0`; clicking **Increment** shows `1`.

`services.lifespan` connects once for the application. `Depends(services)` passes that same connection to each handler, both for the initial HTTP render and connected events. Dependencies require an active application lifespan.

Each view reads the shared counter when it mounts or increments. Other open views do not refresh automatically in this example. Values live in memory and are lost when the program stops. Stop the server with Ctrl+C.

The [full PyView example](../../../examples/pyview/main.py) calls `await services.subscribe(socket, topic, convert)` during connected mount for live updates across views. This returns a `ViewSubscription` owned by the socket's connection. The server must supply connection ownership; an unowned scope raises instead of using application ownership. Retain the handle and await its `aclose()` on disconnect. Do not create subscriptions during the initial HTTP render.
