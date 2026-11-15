# PyBEAM

Python-first tools for BEAM services. Read the [documentation](docs/guide/index.md).

## Examples

```sh
mise run example:fastapi
mise run example:litestar
mise run example:pyview
```

Run one example, then open <http://127.0.0.1:8642/> in two tabs. Increment in either tab. Both update without reloading. `mise run example` defaults to FastAPI. Press Ctrl+C to stop.

To use another address:

```sh
PYBEAM_BIND=127.0.0.1:8660 mise run example
```

The counter lives in BEAM ETS. Updates are atomic. Pub/sub notifications trigger fresh reads of shared state. FastAPI and Litestar use SSE; PyView uses persistent LiveView WebSockets.

The examples run Python inside BEAM through erlang-python and Hornbeam. There is no separate Python server. The counter is public and node-local. Restarting BEAM clears it.

## Development

Use mise for tools and uv for Python dependencies. Runtime development requires sibling checkouts of `hornbeam`, `erlang-python`, `erlang-ws`, and `erlang-h1`. Setup connects them through rebar3's `_checkouts` directory.

```sh
mise run setup
mise run check
mise run test
mise run integration
```

Run browser tests with Chromium:

```sh
uv run playwright install chromium
mise run browser
```

Alternatively, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to an existing browser executable.
