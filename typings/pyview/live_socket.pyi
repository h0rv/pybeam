# Copyright (c) 2026 Anatomy Financial.

from typing import TypeGuard

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pyview.events import InfoEvent
from pyview.instrumentation import InstrumentationProvider
from pyview.live_view import LiveView
from starlette.websockets import WebSocket

class UnconnectedSocket[T]:
    context: T
    connected: bool
    def __init__(self) -> None: ...

class ConnectedLiveViewSocket[T]:
    context: T
    connected: bool
    websocket: WebSocket
    def __init__(
        self,
        websocket: WebSocket,
        topic: str,
        liveview: LiveView[T],
        scheduler: AsyncIOScheduler,
        instrumentation: InstrumentationProvider,
    ) -> None: ...
    async def send_info(self, event: InfoEvent) -> None: ...

type LiveViewSocket[T] = ConnectedLiveViewSocket[T] | UnconnectedSocket[T]

def is_connected[T](socket: LiveViewSocket[T]) -> TypeGuard[ConnectedLiveViewSocket[T]]: ...
