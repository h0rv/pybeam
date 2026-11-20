# PyBEAM

Python services that keep working when one call crashes, share state and messages without Redis or a broker, and stay small in memory.

```sh
uv add python-beam
```

Built on [erlang-python](https://github.com/benoitc/erlang-python), [Hornbeam](https://github.com/benoitc/hornbeam) and [Erlang/OTP](https://www.erlang.org/).

This FastAPI route increments a counter shared by callers in one VM, not a Python variable per worker. No Redis. Install `fastapi` and `uvicorn` alongside PyBEAM:

```python
# counter_app.py
from fastapi import FastAPI
from uvicorn import Config, Server

from pybeam.integrations.fastapi.dependencies import Beam, provider
from pybeam.integrations.hornbeam import run

app = FastAPI(lifespan=provider.lifespan)


@app.post("/increment")
async def increment(beam: Beam) -> dict[str, int]:
    return {"count": await beam.incr("visits", namespace="home")}


if __name__ == "__main__":
    server = Server(Config(app, host="127.0.0.1", port=8767, loop="none"))
    run(server.serve)
```

```console
$ uv run counter_app.py
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8767 (Press CTRL+C to quit)
```

In another terminal:

```console
$ curl -X POST http://127.0.0.1:8767/increment
{"count":1}
$ curl -X POST http://127.0.0.1:8767/increment
{"count":2}
```

## Measured tradeoffs

- **One crash stays in one isolated call:** [0 of 20 clients dropped](tradeoffs.md#one-failure-stays-in-one-call), versus every client on the crashed Uvicorn worker.
- **Counters and pub/sub without Redis:** [400/400 increments and 8/8 subscribers](tradeoffs.md#shared-state-without-redis), versus Uvicorn's 188–286/400 and 1–5/8.
- **Less idle memory:** [165 MiB versus 2.3 GiB](tradeoffs.md#cost-to-run) for the FastAPI+Celery port with 64 task slots.
- **Resume after a node is killed:** [0.13 s versus 144 s](tradeoffs.md#recovery-after-a-node-dies) for Redis/Celery in one recorded run per stack.

The cost: [about 12–38% fewer requests per second](tradeoffs.md#what-pybeam-costs) than tuned Uvicorn/Granian in the HTTP probe. These are small samples, not capacity limits. State is in memory and node-local; isolated calls need a separate OS process.

[Get started](index.md) · [Why PyBEAM](why.md) · [Benefits and tradeoffs](tradeoffs.md)
