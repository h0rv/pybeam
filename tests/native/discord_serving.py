import argparse
import asyncio

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from uvicorn import Config, Server

import pybeam

app = FastAPI()


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    from erlang import ErlangEventLoop

    loop = asyncio.get_running_loop()
    if not isinstance(loop, ErlangEventLoop):
        raise RuntimeError("Expected the managed native event loop")
    return "native-loop-ok"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = Server(Config(app, host="127.0.0.1", port=args.port, loop="none", http="h11"))
    await server.serve()


if __name__ == "__main__":
    pybeam.run(main)
