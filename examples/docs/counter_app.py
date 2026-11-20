# counter_app.py
from fastapi import FastAPI
from uvicorn import Config, Server

from pybeam.integrations.fastapi.dependencies import Beam, provider
from pybeam.integrations.hornbeam import run

app = FastAPI(lifespan=provider.lifespan)


@app.post("/increment")
async def increment(beam: Beam) -> dict[str, int]:
    return {"count": await beam.incr("visits", namespace="home")}


if __name__ == "__main__":
    server = Server(Config(app, host="127.0.0.1", port=8767, loop="none"))
    run(server.serve)
