# docs_pyview.py
import asyncio
from dataclasses import dataclass
from string.templatelib import Template
from typing import override

from pyview.live_socket import LiveViewSocket
from pyview.live_view import LiveView, Session
from pyview.meta import PyViewMeta
from pyview.pyview import PyView
from pyview.template.template_view import TemplateView
from uvicorn import Config, Server

from pybeam.integrations.hornbeam import Beam, run


@dataclass
class CounterState:
    count: int = 0


class CounterView(TemplateView[CounterState], LiveView[CounterState]):
    @override
    async def mount(self, socket: LiveViewSocket[CounterState], session: Session) -> None:
        beam = await Beam.connect()
        count = await beam.incr("visits", 0, namespace="pyview")
        socket.context = CounterState(count)

    async def handle_event(self, event: str, socket: LiveViewSocket[CounterState]) -> None:
        if event == "increment":
            beam = await Beam.connect()
            socket.context.count = await beam.incr("visits", namespace="pyview")

    @override
    def template(self, assigns: CounterState, meta: PyViewMeta) -> Template:
        return t"""<main>
            <p>{assigns.count}</p>
            <button phx-click="increment">Increment</button>
        </main>"""


app = PyView()
app.add_live_view("/", CounterView)


async def main() -> None:
    server = Server(Config(app, host="127.0.0.1", port=8769, loop="none", http="h11"))

    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
