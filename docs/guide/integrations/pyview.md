# PyView

Keep a shared counter behind a PyView page. Each click updates the counter atomically and renders the result with PyView's template engine.

A reconnect runs PyView's required-scope session check again before the connected
mount. A rejected join closes with code `1000` and sends no mounted view; an
accepted reconnect mounts a new view from current state. For missed-message
boundaries, see [Startup, reconnects, and failures](../failures.md#rebuild-a-view-after-reconnect).

```sh
uv add 'python-beam[pyview]' uvicorn
```

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

The [full PyView example](../../../examples/pyview/main.py) calls
`await services.subscribe(socket, topic, convert)` during connected mount for
live updates across views. This works on Uvicorn and PyBEAM's native web server.
Keep the returned `ViewSubscription` and await its `aclose()` on disconnect.
Do not subscribe during the initial HTTP render.

For presence, call `await services.presence(socket, topic, key, payload, convert)`
during connected mount. It delegates to Hornbeam's `Beam.presence` and passes
full snapshots of `tuple[PresenceEntry, ...]` to `convert`. Keep the returned
`ViewPresence` and await `aclose()` on disconnect to acknowledge local untracking.
Its `wait()` method observes consumer completion or failure without taking
cancellation ownership. Like `subscribe`, it accepts `max_bytes=65536`; an
overflow fails the stream rather than silently truncating a snapshot.

On Uvicorn, sockets share the application's limit of 32 active subscriptions
and rely on `aclose()` for disconnect cleanup. The native server owns
subscriptions per connection and can also clean them up when the connection dies.

## Send posts between two local nodes

The [two-node feed example](../../../examples/pyview_cluster/main.py) sends each
post's text and origin node through pub/sub. It does not share a counter or a
history store. From a checkout prepared with the [development setup](../../development.md), run:

```sh
mise run example:pyview-cluster
```

Open [node A](http://127.0.0.1:8771/) and [node B](http://127.0.0.1:8772/) in
separate tabs. Enter a post and select **Send**. Both tabs append the post and
show which node served the sender. Send a second post from the other tab to
see the reverse direction. Press Ctrl+C in the terminal to stop both nodes.

The **Who is online** list shows one entry per connected tab, labeled with its
node and a short tab identifier, plus a count. Open another tab, then close it
to see the list grow and shrink on both nodes. Stopping a node also removes its
tabs from the remaining node's list. A new tab receives the current presence
snapshot, but no earlier posts.

This is a loopback-only local demo, not a deployment configuration. Each
connected tab keeps its latest 20 posts in memory. A new tab's post list starts empty;
reloading clears that tab's list. Messages are not durable: disconnected tabs
and nodes that are down miss posts, with no replay when they reconnect.
There is no authentication; anyone who can access the local ports can post.
Presence is eventually consistent across nodes and is not durable. Join and
leave updates can take time to reach other tabs, especially while a node failure
is being detected. The list is not an instantaneous or authoritative record of
who is connected; restarting the presence engine closes existing presence
streams rather than restoring their entries.
