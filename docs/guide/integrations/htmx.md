# HTMX forms and server-sent events

The [shared counter example](../../../examples/fastapi/README.md) shows how to
combine an HTMX form with server-sent events. When you increment the count in
one tab, that tab shows a confirmation and both tabs update without navigating.

## Give the form and the stream separate targets

Load the matching HTMX 4 core and SSE extension in order, without `async` or
`defer`. The extension must register before HTMX processes the page.

```html
<script src="/static/htmx.min.js"></script>
<script src="/static/sse.js"></script>
```

The form submits to `/increment` and puts the HTML response in `#confirmation`.
The separate `#counter-region` receives HTML from `/events`.

```html
<div id="counter-region" hx-sse:connect="/events" hx-swap="innerHTML" aria-live="polite">
  <output id="count">0</output>
</div>
<form action="/increment" method="post"
      hx-post="/increment" hx-target="#confirmation" hx-swap="innerHTML">
  <label>Amount <input name="amount" type="number" min="1" max="10" value="1" required></label>
  <button type="submit">Increment</button>
</form>
<p id="confirmation" role="status"></p>
```

FastAPI validates the form, increments the PyBEAM counter, and publishes a
change notification. If the request has `HX-Request: true`, the route returns
only the confirmation fragment. A normal form submission redirects to the page.

## Send current state, not a replay

The [SSE route](../../../examples/fastapi/main.py) subscribes to the PyBEAM topic
before reading the counter. It sends an initial snapshot, then reads and renders
the counter again when a notification arrives. FastAPI sends keepalive comments
while the stream is quiet. Each subscription belongs to its HTTP request and
closes when the stream ends.

On reconnect, the route sends a fresh snapshot rather than replaying events.
If the initial connection fails, reload the page to start a new stream. The
example has no authentication, and restarting the server clears the counter.

If the page also uses [PyView](pyview.md), keep the HTMX targets outside the
PyView root. Don't let an HTMX swap replace that root or any of its ancestors.
Share state through PyBEAM, not by having both libraries write to the same DOM.
