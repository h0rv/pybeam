from examples.docs.reconnect import app
from pybeam.integrations.fastapi.dependencies import Beam


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/subscriptions")
async def subscriptions(beam: Beam) -> dict[str, int]:
    return {"count": await beam.subscription_count()}
