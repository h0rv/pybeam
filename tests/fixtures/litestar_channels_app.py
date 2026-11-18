from litestar import get

from examples.docs.litestar_app import app, backend
from pybeam.integrations.hornbeam import Beam


@get("/health")
async def health() -> dict[str, bool]:
    return {"ok": backend.failure is None}


@get("/subscriptions")
async def subscriptions() -> dict[str, int]:
    beam = await Beam.connect()
    return {"count": await beam.subscription_count()}


app.register(health)
app.register(subscriptions)
