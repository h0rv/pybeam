import os
import sys
import time
from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

from pybeam.runtime import guardian


def boundary(directory: Path) -> None:
    (directory / "boundary").touch()
    deadline = time.monotonic() + 4
    while not (directory / "release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Scheduling barrier was not released")
        time.sleep(0.01)


def main() -> None:
    directory = Path(os.environ["PYBEAM_OWNER_DIRECTORY"])
    marker = directory / "entry"
    entry = (
        "from pathlib import Path; from pybeam.codec import Json; "
        "from pybeam.runtime.ownership import Identity, resolve; "
        "owner=Json(Identity).decode(" + repr(os.environ.get("PYBEAM_OWNER_IDENTITY")) + "); "
        "Path("
        + repr(str(marker))
        + ").write_text('live' if resolve(owner) is not None else 'gone')"
    )
    command = [sys.executable, "-c", entry]
    match sys.argv[1]:
        case "before_grant":
            authorize = guardian._authorize_execution

            def delayed(registration: guardian.Registration) -> None:
                boundary(directory)
                authorize(registration)

            with patch.object(guardian, "_authorize_execution", delayed):
                guardian.gate(command)
        case "after_grant":
            execute = os.execv

            def delayed_exec(path: str, argv: list[str]) -> NoReturn:
                boundary(directory)
                execute(path, argv)

            with patch.object(guardian.os, "execv", delayed_exec):
                guardian.gate(command)
        case "direct":
            guardian.gate(command)
        case _:
            raise ValueError("Unknown scheduling barrier")


if __name__ == "__main__":
    main()
