# Get started

PyBEAM adds actors, shared state and messaging to async Python, without locks or extra infrastructure. Keep your existing web framework and write ordinary async functions.

```sh
uv add python-beam
```

```python
# hello.py
import pybeam


async def main() -> None:
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
```

```console
$ uv run hello.py
Hello from Python
```

`pybeam.run` starts PyBEAM, runs `main`, and shuts down when `main` returns. Pass the function, not `main()`. Inside `main`, write ordinary async Python. PyBEAM requires Python 3.14.

## Next steps

- [Actors](actors.md): keep state in an object that handles one call at a time.
- [Supervision](supervision.md): restart an actor that fails.
- [Shared state](state.md): store values and count across requests.
- [Messaging](messaging.md): notify subscribers.
- [Background services](background.md): run a long-lived loop and stop it cleanly.
- [Durable tasks](durable-tasks.md): record retryable work across process crashes.
- Integrations: [FastAPI](integrations/fastapi.md), [Litestar](integrations/litestar.md) or [PyView](integrations/pyview.md).

To see why you would use these, read [Why PyBEAM](why.md).
