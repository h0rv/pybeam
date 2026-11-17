# PyBEAM

Actors, shared state and messaging for async Python, without locks or extra infrastructure.

```sh
uv add python-beam
```

- [Actors](docs/guide/actors.md) keep state and handle one call at a time, so you never need a lock.
- [Supervision](docs/guide/supervision.md) restarts an actor that fails.
- [Shared state](docs/guide/state.md) stores values and counts atomically across requests.
- [Messaging](docs/guide/messaging.md) notifies subscribers without a broker.
- [Background services](docs/guide/background.md) run long-lived loops that stop cleanly.
- [Web integration](docs/guide/web.md) works with FastAPI, Litestar and PyView.

Start with [Get started](docs/guide/index.md), or read [Why PyBEAM](docs/guide/why.md).

Try the [Discord demo](examples/discord_demo/README.md) and the [showcase](examples/showcase).

To work on PyBEAM itself, see [Development](docs/development.md).
