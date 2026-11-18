import asyncio
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from sqlalchemy import String, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

import pybeam

type Mode = Literal["sync", "async"]


class Base(DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[Annotated[int, mapped_column(primary_key=True)]]
    text: Mapped[Annotated[str, mapped_column(String(200), unique=True)]]


@dataclass(frozen=True)
class Command:
    mode: Mode
    text: str
    fail: bool = False


class Database:
    def __init__(self, directory: Path) -> None:
        self.loop = asyncio.get_running_loop()
        self.sync = create_engine(
            "sqlite:///" + str(directory / "sync.sqlite3"),
            connect_args={"check_same_thread": False, "timeout": 10},
            pool_size=2,
            max_overflow=0,
        )
        self.asynchronous = create_async_engine(
            "sqlite+aiosqlite:///" + str(directory / "async.sqlite3"),
            connect_args={"timeout": 10},
            pool_size=2,
            max_overflow=0,
        )
        self.sync_sessions = sessionmaker(self.sync)
        self.async_sessions = async_sessionmaker(self.asynchronous)

    async def start(self) -> None:
        await asyncio.to_thread(Base.metadata.create_all, self.sync)
        async with self.asynchronous.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    def sync_add(self, command: Command) -> str:
        with self.sync_sessions.begin() as session:
            session.add(Note(text=command.text))
            session.flush()
            if command.fail:
                raise ValueError("rollback requested")
        return command.text

    async def async_add(self, command: Command) -> str:
        async with self.async_sessions.begin() as session:
            session.add(Note(text=command.text))
            await session.flush()
            if command.fail:
                raise ValueError("rollback requested")
        return command.text

    async def on_owner[T](self, operation: Coroutine[object, object, T]) -> T:
        if asyncio.get_running_loop() is self.loop:
            return await operation
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(operation, self.loop))

    async def add(self, command: Command) -> str:
        if command.mode == "sync":
            return await asyncio.to_thread(self.sync_add, command)
        return await self.on_owner(self.async_add(command))

    async def read(self, mode: Mode) -> list[str]:
        if mode == "sync":
            return await asyncio.to_thread(self.sync_read)
        return await self.on_owner(self.async_read())

    def sync_read(self) -> list[str]:
        with self.sync_sessions() as session:
            return list(session.scalars(select(Note.text).order_by(Note.text)))

    async def async_read(self) -> list[str]:
        async with self.async_sessions() as session:
            return list(await session.scalars(select(Note.text).order_by(Note.text)))

    async def close(self) -> None:
        await self.asynchronous.dispose()
        await asyncio.to_thread(self.sync.dispose)


databases: dict[str, Database] = {}


@asynccontextmanager
async def database(directory: Path) -> AsyncGenerator[Database]:
    db = Database(directory)
    try:
        await db.start()
        databases["notes"] = db
        yield db
    finally:
        databases.pop("notes", None)
        await db.close()
        print("sqlalchemy-disposed", flush=True)


async def add_note(command: Command) -> str:
    return await databases["notes"].add(command)


@pybeam.actor
class Notebook:
    async def add(self, command: Command) -> str:
        return await add_note(command)

    async def read(self, mode: Mode) -> list[str]:
        return await databases["notes"].read(mode)
