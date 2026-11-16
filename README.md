# PyBEAM

Run async Python on BEAM with supervised actors and shared messages.

## When to use PyBEAM

PyBEAM is for Python developers who need supervised stateful work and shared
services alongside async applications. Keep plain asyncio for local concurrency;
PyBEAM adds OTP lifecycle management and explicit admission limits.

- [Actors](docs/guide/actors.md) own Python state and serialize method calls.
  [OTP supervision](docs/guide/supervision.md) restarts failed actors with fresh
  state; acquire a new reference rather than retrying the old one.
- Bounded admission limits queued and running actor requests and encoded bytes,
  not total memory. Timeouts do not undo effects.
- [Hornbeam services](docs/guide/state.md) provide shared key/value storage and
  atomic counters; [pub/sub](docs/guide/messaging.md) sends volatile notifications.
- [Background services](docs/guide/background.md) and tasks support owned async
  work; [web adapters](docs/guide/web.md) integrate FastAPI, Litestar and PyView.
- PyBEAM owns VM startup and shutdown through `pybeam.run`.

Python uses one main interpreter and its GIL: no multicore Python execution or
C-extension crash isolation. OTP supervision does not change those limits.

Read [Get started](docs/guide/index.md), then try [actors](docs/guide/actors.md) or [shared state](docs/guide/state.md).

Explore the runnable [Discord demo](examples/discord_demo/README.md) and
[showcase](examples/showcase).
