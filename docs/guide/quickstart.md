# Quickstart

## Install a provided wheel

The distribution is `python-beam`; the import is `pybeam`. It is not published on PyPI. Obtain a trusted local platform wheel before trying this path. Replace the example directory below with a directory containing exactly that wheel.

```sh
uv init --python 3.14.10 actor-demo
cd actor-demo
uv add /absolute/path/to/python_beam-*.whl
```

The current packaging target is conventional CPython 3.14 on macOS arm64. Use CPython 3.14.10 for the development baseline. Your macOS version must meet the wheel tag and bundled manifest minimum. The wheel bundles ERTS and native runtime assets, but your Python installation must provide `libpython3.14.dylib`. Free-threaded Python is not supported by this wheel.

Install only artifacts you trust. The wheel includes native executables and libraries. An editable source installation alone does not supply the embedded runtime.

## Run an actor

Save the following as `main.py`. It is the repository's [actor example](../../examples/actors/main.py).

```python
import asyncio
import itertools

import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, n: int) -> int:
        self.value += n
        return self.value


async def main() -> None:
    async with pybeam.ActorGroup() as actors:
        first = await actors.spawn(Counter, value=0)
        print(await first.add(1))
        counters: list[Counter] = await asyncio.gather(
            *map(actors.spawn, itertools.repeat(Counter, 5))
        )
        print(await asyncio.gather(*(counter.add(1) for counter in counters)))
    async with pybeam.spawn(Counter, value=10) as counter:
        print(await counter.add(2))


if __name__ == "__main__":
    pybeam.run(main)
```

```sh
uv run python main.py
```

Expect `1`, then `[1, 1, 1, 1, 1]`, then `12`. These values demonstrate separate state, not performance. `pybeam.run(main)` loads the file again inside the managed VM. Pass the async function, not `main()`. Do not rely on objects or closures from the launching interpreter.

## Work from source

Contributors use mise and the repository's locked Python dependencies. The source workflow requires the sibling runtime checkouts described in the [repository README](../../README.md). `mise run setup` explicitly builds native assets; it is not a consumer install command.

With an already built compatible runtime, configure `[tool.pybeam].development_runtime` to its build directory before running the actor example. Missing or incompatible assets cause startup to fail. There is no fallback to local Python actors.
