# Get started

Add actors, shared state and messaging to async Python.

```sh
uv add python-beam
```

```python
import pybeam


async def main() -> None:
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
```

Save this as `main.py` and run `python main.py`. It prints `Hello from Python`.

`pybeam.run` starts PyBEAM, runs `main`, and shuts down when `main` returns. Pass the function, not `main()`. Inside `main`, write ordinary async Python. PyBEAM requires Python 3.14.

## Next steps

- [Actors](actors.md): keep state in an object that handles one call at a time.
- [Supervision](supervision.md): restart an actor that fails.
- [Shared state](state.md): store values and count across requests.
- [Messaging](messaging.md): notify subscribers.
- [Background services](background.md): run a long-lived loop and stop it cleanly.
- [Web integration](web.md): use these from FastAPI, Litestar or PyView.

To see why you would use these, read [Why PyBEAM](why.md).
