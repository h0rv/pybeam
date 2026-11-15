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

Read the [getting started guide](docs/guide/quickstart.md), then try [actors](docs/guide/actors.md), [messaging](docs/guide/messaging.md), or the [web integration](docs/guide/web.md).
