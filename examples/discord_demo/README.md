# Native room miniapp

From the repository root:

```sh
uv run --extra fastapi --extra hornbeam python -m examples.discord_demo.main
```

Open `http://127.0.0.1:8766` in two separate browser sessions. Use `--port` to choose another loopback port. Join a room before sending. The first browser owns the crash button. Crashing replaces that room's BEAM child with empty state. Everyone must explicitly join the new generation.

This example needs a managed Python 3.14 runtime with native actor groups and Hornbeam's supervised services and registered service callback. The current managed launcher does not initialize those Hornbeam services. Startup therefore stops with a capability error. It does not start a second server or substitute polling or Python room state.

Leave is disabled until the room domain provides its native leave method. Disconnect releases the subscription, not membership. Changing rooms navigates to a new page and closes the old stream.

Rooms are limited to eight members and 100 events per generation. Only the latest 32 messages remain in history. Messages are limited to 256 characters and 1024 UTF-8 bytes. Native subscription queues are bounded. Slow output disconnects after five seconds. Failed or cancelled writes are never automatically retried and can have effects. There is no durability or Discord authentication.
