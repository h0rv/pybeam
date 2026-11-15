# PyBEAM

PyBEAM exposes BEAM and OTP primitives through Python APIs.

Start with an async Python function and `pybeam.run(main)`. PyBEAM owns runtime startup and shutdown. Add BEAM capabilities where your application needs them.

Use functions for work and classes for state. Await results, exchange messages, and own lifetimes with async context managers. OTP provides process supervision. Hornbeam provides HTTP serving and BEAM services for web applications.

## Python semantics, BEAM primitives

| Capability | Python mapping |
| --- | --- |
| Background jobs and tasks | Async functions perform work. Handles represent completion and cancellation under an owner. |
| Channels | Typed sends and receives exchange data through a bounded queue. Async iteration consumes a stream. |
| Message passing | Requests return results. Publish/subscribe sends notifications to interested receivers. |
| Actors | Classes own state. Callers await typed methods on references. |
| Supervision | Owned scopes define lifetimes. Restart policies define which failed processes are replaced. |
| Web and integrations | ASGI lifespans and dependencies connect HTTP handlers to services and live updates. |

## Compose an application

A background service can receive work, call an actor that owns shared data, and publish a completion notification. An HTTP handler can use the same services without owning the background work's lifetime. Choose each primitive for its role rather than putting the entire application in an actor.

The [quickstart](quickstart.md) begins with ordinary async Python. The [core primitives](primitives.md) guide explains how to add messaging and supervised work. [Web and integrations](web.md) covers Hornbeam services and GenStage streams.

Ordinary `asyncio` is enough when you only need concurrent I/O within Python task scopes. Use PyBEAM when you also need BEAM messaging or OTP lifetimes. Read the [boundaries](limits.md) for the execution and delivery semantics.
