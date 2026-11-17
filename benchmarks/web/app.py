from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()


@app.get("/")
async def index() -> dict[str, str]:
    return {"message": "hello"}


@app.websocket("/ws")
async def echo(socket: WebSocket) -> None:
    await socket.accept()
    try:
        while True:
            await socket.send_text(await socket.receive_text())
    except WebSocketDisconnect:
        return
