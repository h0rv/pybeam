# PyBEAM

Actors, shared state and messaging for async Python, without a separate broker.

```sh
uv add python-beam
```

- [Actors](docs/guide/actors.md) keep state and handle one call at a time, without locks around actor state.
- [Supervision](docs/guide/supervision.md) restarts an actor that fails.
- [Shared state](docs/guide/state.md) stores values and counts atomically across requests.
- [Messaging](docs/guide/messaging.md) notifies subscribers without a broker.
- [Background services](docs/guide/background.md) run long-lived loops that stop cleanly.
- Integrations work with [FastAPI](docs/guide/integrations/fastapi.md), [Litestar](docs/guide/integrations/litestar.md) and [PyView](docs/guide/integrations/pyview.md).

Start with [Get started](docs/guide/index.md), or read [Why PyBEAM](docs/guide/why.md).

Try the [Discord demo](examples/discord_demo/README.md) and the [showcase](examples/showcase).

The [live-operations walkthrough](examples/soul/README.md) inspects and stops
runaway Python work while HTTP traffic continues, then replaces the calculation
used by new jobs.

To work on PyBEAM itself, see [Development](docs/development.md).
