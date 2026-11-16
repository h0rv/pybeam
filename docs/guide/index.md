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
  support macOS arm64 and Linux x86_64; `python-beam` is not published to PyPI yet.
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

[Why PyBEAM](why.md) · [Actors](actors.md) · [Supervision](supervision.md) · [Shared state](state.md) · [Messaging](messaging.md) · [Background services](background.md) · [Web integration](web.md)

## Choose a primitive

- [Actor](actors.md): Python state plus methods.
- [Hornbeam store and `incr`](state.md): shared key/value storage and counters without an actor.
- [Publish/subscribe](messaging.md): notifications to subscribers.
- [`ServiceSlot`](background.md): a long-running background loop.
- [Web adapters](web.md): connect FastAPI, Litestar or PyView to BEAM services.

Pass the function to `pybeam.run`, not the coroutine `main()`. PyBEAM starts and stops the runtime. Ordinary asyncio tasks work inside `main`; they are not OTP processes.

Python uses one main interpreter and its GIL. OTP processes do not isolate C-extension crashes or provide multicore Python execution.

## Linux x86_64

Linux wheels use the `cp314-cp314-linux_x86_64` tag, not a manylinux tag. Use conventional CPython 3.14 with a shared `libpython3.14.so.1.0`. The runtime uses that interpreter's library and standard library. The system must provide glibc, libm, libstdc++, libgcc_s, libtinfo.so.6, zlib and libcrypto.so.3. This build is verified on Ubuntu 22.04; the tag does not promise compatibility with every Linux distribution.

### Build a wheel

Install the tools in `mise.toml` and sync the locked dependencies.

```sh
mise install
mise exec -- uv sync --locked --all-extras
mise exec -- uv run --locked python tools/build_native.py
mkdir -p build
curl -fL https://raw.githubusercontent.com/erlang/otp/OTP-29.1.2/LICENSE.txt -o build/OTP-LICENSE.txt
mise exec -- uv run --locked python tools/build_runtime.py \
  --otp-root "$(mise where erlang)" \
  --native-app _build/default/lib/erlang_python \
  --otp-license build/OTP-LICENSE.txt \
  --native-license _build/default/lib/erlang_python/LICENSE
mise exec -- uv build --wheel
```

Linux wheels do not bundle the optional GenStage payload. Calling the GenStage integration reports that the payload is not bundled on Linux. Core actors and shared state do not require it. Darwin wheels retain their pinned GenStage payload.

Install `dist/python_beam-0.1.0a1-cp314-cp314-linux_x86_64.whl` into a Python 3.14 environment. Running `python examples/hello.py` or `python examples/actors/main.py` then starts and stops the bundled runtime without `erl`, rebar3 or mise on PATH.

### Use a development runtime

For local work without packaging, install the mise tools, sync dependencies and run `tools/build_native.py` as above. Add this to the consumer project's `pyproject.toml`, using an absolute path or a path relative to that file:

```toml
[tool.pybeam]
development_runtime = "/path/to/pybeam/_build/hornbeam"
```

Run `python examples/hello.py` with the project environment and mise tools active. This path requires the already-built native runtime, an installed `erl`, and the same conventional Python ABI used to build the NIF. It does not build or download a runtime implicitly.
