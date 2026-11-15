# PyBEAM

Python actors and shared state on BEAM.

PyBEAM runs Python inside the Erlang VM through erlang-python. You write typed Python methods. OTP owns the actor processes and supervision. PyBEAM provides Python interfaces and controls runtime startup and shutdown.

Use it when several connections need the same state owner, or when a failed actor needs a supervised replacement with an explicit lifetime. A room can own its membership and messages instead of sharing a mutable Python object between request handlers.

## The programming model

Declare an actor with `@pybeam.actor`. Spawn it inside an `ActorGroup`, then await its methods. Each actor handles calls serially, including while a method awaits. Arguments and results cross a serialized data boundary. The reference exposes methods, not the actor's fields.

Separate actors have separate state. They do not each get an isolated CPython process. The current baseline is one VM with the main Python interpreter and its GIL.

For web applications, keep your ASGI framework. The examples use BEAM state with FastAPI, Litestar, or PyView. PyBEAM is not a replacement web framework or database.

## Start here

Follow the [quickstart](quickstart.md) for installation requirements and an actor program. Read [actors and state](actors.md) before adding concurrent callers. The [web examples](web.md) show shared counters and a small room application.

This is an early library. It is not published on PyPI. A supported local platform wheel is required for the consumer path. Read the [boundaries](limits.md) before choosing it for an application.

Ordinary `asyncio` is enough when your application only needs concurrent I/O with task lifetimes inside one Python process. Adding BEAM is useful only if you need its state ownership or OTP behavior and accept the runtime integration cost.
