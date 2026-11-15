# PyBEAM

PyBEAM runs Python actors inside the Erlang VM. You write typed async methods. OTP owns the actor processes and supervision.

Use an actor when several callers need one state owner. A room can own its membership and messages instead of sharing a mutable Python object between request handlers. Callers await methods on a reference. They cannot read or write the actor's fields.

## The programming model

Declare a class with `@pybeam.actor`, spawn it inside an `ActorGroup`, then await its methods. Each actor handles calls serially, including while a method awaits. Arguments and results are serialized copies, not shared Python objects.

The group owns its actors' lifetimes. OTP can replace a failed actor with fresh state. Callers explicitly acquire a reference to the new generation rather than silently retrying a failed call.

## Use PyBEAM

Start with the [counter example](quickstart.md). Read [actors and state](actors.md) for admission limits and restart behavior. The [web examples](web.md) show how to use shared BEAM state from ASGI applications.

Ordinary `asyncio` is enough for concurrent I/O when you do not need BEAM state ownership or OTP supervision. See the [boundaries](limits.md) for the execution and persistence tradeoffs.
