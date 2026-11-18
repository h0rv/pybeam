import argparse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import IntegrityError

from examples.sqlalchemy.store import Command, Mode, Notebook, add_note, database, databases
from pybeam import ActorGroup
from pybeam.codec import Json
from pybeam.integrations.hornbeam import run
from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with database(Path.cwd()):
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health() -> str:
    return "ok"


@app.get("/notes/{mode}")
async def read(mode: Mode) -> list[str]:
    return await databases["notes"].read(mode)


@app.post("/notes")
async def add(command: Command) -> str:
    try:
        return await add_note(command)
    except (ValueError, IntegrityError) as error:
        raise HTTPException(409, "Transaction rolled back") from error


@app.post("/actors")
async def actor_add(command: Command) -> str:
    async with ActorGroup() as actors:
        notebook = await actors.spawn(Notebook)
        return await notebook.add(command)


@app.post("/tasks/{mode}")
async def fan_out(mode: Mode, prefix: str) -> list[str]:
    function = TaskFunction(add_note, Json(Command), Json(str), dedicated_loop=True)
    async with TaskSupervisor(TaskLimits(capacity=4, max_bytes=4096, shutdown_ms=5000)) as tasks:
        refs = [await tasks.start(function, Command(mode, prefix + str(i))) for i in range(4)]
        return [await ref for ref in refs]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, loop="none", http="h11")
    )
    await server.serve()


if __name__ == "__main__":
    run(main)
