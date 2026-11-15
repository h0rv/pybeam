# Native room miniapp

From the repository root:

```sh
uv run --extra fastapi --extra hornbeam python -m examples.discord_demo.main
```

Open `http://127.0.0.1:8766` in two separate browser sessions. Use `--port` to choose another loopback port. Join a room before sending. The first browser owns the crash button. Crashing replaces that room's BEAM child with empty state. Everyone must explicitly join the new generation.

This example needs locally staged Python 3.14 native assets with actor groups and opt-in Hornbeam services. The source installation alone does not provide those assets. The Hornbeam integration owns startup and shutdown of its existing supervised services and callback. It does not start Hornbeam's HTTP listener. Uvicorn serves FastAPI inside the managed native event loop. Missing runtime capabilities stop startup rather than substituting polling or Python room state.

Leave stops native delivery receipts for your membership. The live view remains open. Rejoin explicitly to receive deliveries again. Leave forms capture the native membership revision so an old form cannot remove a newer membership. Disconnect releases the subscription, not membership. Changing rooms navigates to a new page and closes the old stream.

Chrome acceptance is opt-in, separate from unit tests:

```sh
uv run --extra fastapi --extra hornbeam pytest tests/test_discord_demo_browser.py --run-frameworks --run-runtime --run-browser
```

An explicitly requested browser run fails if Chrome or the native runtime is unavailable. Without the flags, browser tests are deselected, not reported as passing.

Rooms are limited to eight distinct identities and 100 events per generation. Departed identities still occupy slots until restart; the same identity can rejoin. Only the latest 32 messages remain in history. Messages are limited to 256 characters and 1024 UTF-8 bytes. Native subscription queues are bounded. Slow output disconnects after five seconds. Failed or cancelled writes are never automatically retried and can have effects. There is no durability or Discord authentication.
