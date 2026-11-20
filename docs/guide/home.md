# PyBEAM

Build resilient Python services with a small runtime footprint.

```sh
uv add python-beam
```

Built on [erlang-python](https://github.com/benoitc/erlang-python), [Hornbeam](https://github.com/benoitc/hornbeam) and [Erlang/OTP](https://www.erlang.org/).

- An isolated call contains a native crash while its caller keeps running.
- Shared state and pub/sub live in the runtime.
- Actor snapshots and durable tasks persist and replicate through Mnesia.
- Services keep a small memory footprint.

[Get started](index.md) · [Why PyBEAM](why.md) · [Benefits and tradeoffs](tradeoffs.md) · [Crash containment](examples/crash-containment.md)
