# reconnect.py
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from uvicorn import Config, Server

from pybeam.integrations.fastapi.dependencies import Beam, provider
from pybeam.integrations.hornbeam import run

app = FastAPI(lifespan=provider.lifespan)


@app.post("/increment")
async def increment(beam: Beam) -> dict[str, int]:
    count = await beam.incr("visits", namespace="reconnect")
    await beam.publish("reconnect:changed", b"invalidate")
    return {"count": count}


async def send_updates(socket: WebSocket, beam: Beam) -> None:
    async with beam.subscribe("reconnect:changed", max_bytes=4096) as changes:
        count = await beam.incr("visits", 0, namespace="reconnect")
        await socket.send_json({"count": count})
        async for _message in changes:
            count = await beam.incr("visits", 0, namespace="reconnect")
            await socket.send_json({"count": count})


@app.websocket("/counter")
async def counter(socket: WebSocket, beam: Beam) -> None:
    if socket.query_params.get("token") != "demo-token":
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        async with asyncio.TaskGroup() as tasks:
            sending = tasks.create_task(send_updates(socket, beam))
            try:
                async for _message in socket.iter_text():
                    pass
            finally:
                sending.cancel()
    except* WebSocketDisconnect:
        pass


async def main() -> None:
    config = Config(app, host="127.0.0.1", port=8770, loop="none", http="h11")
    server = Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
