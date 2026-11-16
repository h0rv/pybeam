# Get started

Run async Python on BEAM with supervised actors and shared messages.

```python
import pybeam


async def main() -> None:
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
```

Save this as `main.py` and run `python main.py`. It prints `Hello from Python`.

## Requirements

Use conventional CPython 3.14. `pybeam.run` needs a managed runtime:

- A `python-beam` wheel with bundled runtime assets. These wheels currently
  support macOS arm64 only; `python-beam` is not published to PyPI yet.
- An already-built compatible development runtime, selected with
  `development_runtime` under `[tool.pybeam]` in `pyproject.toml`. Set it to the
  build profile directory (for example, `_build/hornbeam`, relative to
  `pyproject.toml`), not an executable or source checkout. It must contain
  `lib/*/ebin` or `checkouts/*/ebin` directories
  with `py_launch.beam` and `erlang_python.app`, and exactly one matching
  `py_nif.so` or `py_nif.dylib` in that application's sibling `priv` directory.
  The native library must match the host Python; `erl` must be on `PATH`.

A source install alone does not include ERTS or the native extension.

Dependency sync and type checks work from a clean checkout without sibling
repositories; see [Development](../development.md). Sibling checkouts are an
opt-in for runtime development: building the native runtime locally requires
`hornbeam`, `erlang-python`, `erlang-ws` and `erlang-h1` next to the PyBEAM
repository. Native builds use
CPython 3.14.10, Erlang 29.1.2, rebar3 3.28.0 and CMake 4.5.0 as pinned in
`mise.toml`. With these tools available, build with `mise run setup`.

[Actors](actors.md) · [Supervision](supervision.md) · [Shared state](state.md) · [Messaging](messaging.md) · [Background services](background.md) · [Web integration](web.md)

## Choose a primitive

- [Actor](actors.md): Python state plus methods.
- [Hornbeam store and `incr`](state.md): shared key/value storage and counters without an actor.
- [Publish/subscribe](messaging.md): notifications to subscribers.
- [`ServiceSlot`](background.md): a long-running background loop.
- [Web adapters](web.md): connect FastAPI, Litestar or PyView to BEAM services.

Pass the function to `pybeam.run`, not the coroutine `main()`. PyBEAM starts and stops the runtime. Ordinary asyncio tasks work inside `main`; they are not OTP processes.

Python uses one main interpreter and its GIL. OTP processes do not isolate C-extension crashes or provide multicore Python execution.
