# PyBEAM

Run async Python on BEAM, with OTP supervision and messaging available when you need them.

```python
import pybeam


async def main() -> None:
    print("Hello from Python")


if __name__ == "__main__":
    pybeam.run(main)
```

This prints `Hello from Python`. PyBEAM owns runtime startup and shutdown.

[Get started](quickstart.md) · [Actors](actors.md) · [Messaging](messaging.md) · [State](state.md)

Use [subscriptions](subscriptions.md) for live updates, [background services](background.md) for owned work, and [GenStage](genstage.md) for demand-driven processing. Connect HTTP handlers with the [web integration](web.md).
