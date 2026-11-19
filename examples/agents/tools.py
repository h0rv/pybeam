import ctypes
import json
import os
import time
from pathlib import Path

from pydantic import TypeAdapter

from examples.agents.protocol import Execution, ToolRequest


def execute(request: ToolRequest) -> str:
    directory = Path(request.audit_directory)
    directory.mkdir(parents=True, exist_ok=True)
    event = Execution(pid=os.getpid(), entered=time.monotonic())
    with (directory / (request.session + "." + request.call_id)).open("ab", buffering=0) as audit:
        audit.write(event.model_dump_json().encode() + b"\n")
    match request.name:
        case "lookup":
            time.sleep(1)
            return "Lookup result for " + request.call_id
        case "hang":
            while True:
                time.sleep(1)
        case "burn":
            deadline = time.monotonic() + 30
            total = 0
            while time.monotonic() < deadline:
                total += 1
            return "CPU work finished: " + str(total) + " iterations"
        case "crash":
            ctypes.string_at(0)
            raise AssertionError


async def isolated(payload: bytes) -> bytes:
    result = execute(ToolRequest.model_validate_json(TypeAdapter(str).validate_json(payload)))
    return json.dumps(result).encode()


def executions(directory: Path, session: str) -> dict[str, list[Execution]]:
    return {
        path.name.removeprefix(session + "."): [
            Execution.model_validate_json(line) for line in path.read_bytes().splitlines()
        ]
        for path in directory.glob(session + ".*")
    }
