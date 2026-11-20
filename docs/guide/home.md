# PyBEAM

<!-- Logo slot: place the mark beside the heading when an asset is available. -->

Isolate failing calls, share state and messages without Redis or a broker, and keep idle memory small.

Built on [erlang-python](https://github.com/benoitc/erlang-python), [Hornbeam](https://github.com/benoitc/hornbeam) and [Erlang/OTP](https://www.erlang.org/).

```sh
uv add python-beam
```

A supervisor replaces a crashed actor with fresh state. The caller continues:

```python
# recovery.py
import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def increment(self) -> int:
        self.value += 1
        return self.value


async def main() -> None:
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter)
        print("Before crash:", await counter.increment())
        await actors.crash(counter)
        replacement = await actors.rejoin(counter)
        print("After restart:", await replacement.increment())
    print("Caller keeps running")


if __name__ == "__main__":
    pybeam.run(main)
```

The supervisor also emits a crash diagnostic; application output:

```console
$ uv run recovery.py
Before crash: 1
After restart: 1
Caller keeps running
```

Restarting an actor loses its state and in-flight calls; it does not replay methods. Native-crash containment requires [isolated tasks](execution.md).

[Get started](index.md) · [Why PyBEAM](why.md) · [Benefits and tradeoffs](tradeoffs.md)
