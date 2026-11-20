import ctypes
import json
import os
import select
import time
from pathlib import Path

from pydantic import TypeAdapter

from examples.agents import runtime
from examples.agents.app import app
from examples.agents.protocol import Execution, ToolRequest
from examples.agents.tools import isolated
from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskFunction

__all__ = ["app"]


async def gated(payload: bytes) -> bytes:
    request = ToolRequest.model_validate_json(TypeAdapter(str).validate_json(payload))
    if request.name not in {"crash", "burn"}:
        return await isolated(payload)
    directory = Path(request.audit_directory)
    with (directory / "gates" / request.name).open("r+b", buffering=0) as release:
        event = Execution(pid=os.getpid(), entered=time.monotonic())
        with (directory / (request.session + "." + request.call_id)).open(
            "ab", buffering=0
        ) as audit:
            audit.write(event.model_dump_json().encode() + b"\n")
        if request.name == "crash":
            select.select([release], [], [])
            ctypes.string_at(0)
            raise AssertionError("Crash tool survived")
        total = 0
        while not select.select([release], [], [], 0)[0]:
            total += 1
    return json.dumps("CPU work finished: " + str(total) + " iterations").encode()


runtime.TOOL = TaskFunction(gated, Json(str), Json(str), execution=ExecutionMode.ISOLATED)
