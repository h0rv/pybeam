# Shared counter

This example shows a FastAPI form and a server-sent event stream on Hornbeam.
When two tabs show the page, an increment updates the counter in both tabs
through PyBEAM pub/sub. Only the submitting tab receives the form's HTML
confirmation.

See the [HTMX guide](../../docs/guide/integrations/htmx.md) for the page markup
and stream behavior.
