# Shared counter

This example serves a FastAPI form and a server-sent event stream on Hornbeam.
After [setting up the development runtime](../../docs/development.md), run from
the repository root:

```sh
mise run serve -- examples.fastapi.main:app
```

Open `http://127.0.0.1:8642/` in two tabs and increment in one. The form's HTML
response confirms the increment in that tab; the PyBEAM pub/sub notification
updates the counter in both tabs.

See the [HTMX guide](../../docs/guide/integrations/htmx.md) for the page markup
and stream behavior.
