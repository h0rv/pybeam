import os
import time
from dataclasses import dataclass
from typing import Literal, assert_never

type FaultKind = Literal["raise", "hog", "abort"]


class RoomFault(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FaultRequest:
    kind: FaultKind
    seconds: float


def trigger(request: FaultRequest) -> None:
    match request.kind:
        case "raise":
            raise RoomFault("Injected room handler failure")
        case "hog":
            deadline = time.perf_counter() + request.seconds
            while time.perf_counter() < deadline:
                pass
        case "abort":
            os.abort()
        case _ as unreachable:
            assert_never(unreachable)


async def worker_fault(request: FaultRequest) -> None:
    trigger(request)


async def abort_entry(payload: bytes) -> bytes:
    trigger(FaultRequest("abort", 0))
    return b"null"
