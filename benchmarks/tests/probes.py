import asyncio
import os
import sys
import threading
import time
from pathlib import Path

from pybeam.codec import Json

from benchmarks.contract import AdapterResult, Config
from benchmarks.ownership import spawn_owned
from benchmarks.python_smoke import run


def main() -> None:
    mode = sys.argv[1]
    directory = Path(sys.argv[2])
    if mode in {"child_exit", "child_wait", "false_cleanup"}:
        child = spawn_owned(directory, (sys.executable, "-c", "import time; time.sleep(60)"))
        (directory / "test-child.json").write_text(Json(type(child)).encode(child).decode())
        if mode == "child_exit":
            os._exit(7)
    if mode in {"false_cleanup", "bad_evidence"}:
        config = Config.model_validate_json((directory / "config.json").read_text())
        output = asyncio.run(run(config))
        if not isinstance(output, AdapterResult):
            raise RuntimeError("Expected the scalar smoke result")
        if mode == "bad_evidence":
            output = output.model_copy(update={"receipts": (output.receipts[0],) * 1000})
        (directory / "adapter.json").write_text(output.model_dump_json())
        return
    if mode == "threads":
        for _ in range(3):
            threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
    if mode == "artifact":
        sys.stdout.write("x" * 1_000_000)
        sys.stdout.flush()
    if mode == "memory":
        allocation = bytearray(64 * 1024 * 1024)
        allocation[0] = 1
    time.sleep(60)


if __name__ == "__main__":
    main()
