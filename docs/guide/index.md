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

[Actors](actors.md) · [Supervision](supervision.md) · [Shared state](state.md) · [Messaging](messaging.md) · [Background services](background.md) · [Web integration](web.md)

## Choose a primitive

- [Actor](actors.md): Python state plus methods.
- [Hornbeam store and `incr`](state.md): shared key/value storage and counters without an actor.
- [Publish/subscribe](messaging.md): notifications to subscribers.
- [`ServiceSlot`](background.md): a long-running background loop.
- [Web adapters](web.md): connect FastAPI, Litestar or PyView to BEAM services.

Pass the function to `pybeam.run`, not the coroutine `main()`. PyBEAM starts and stops the runtime. Ordinary asyncio tasks work inside `main`; they are not OTP processes.

Python uses one main interpreter and its GIL. OTP processes do not isolate C-extension crashes or provide multicore Python execution.
