import inspect
import sys
from pathlib import Path

from pybeam.runtime.assets import discover_assets
from pybeam.runtime.config import BootstrapError, load_config
from pybeam.runtime.loader import Entry, register
from pybeam.runtime.process import launch


def run_file(path: Path, args: list[str]) -> int:
    entry = path.resolve()
    if not entry.is_file():
        raise BootstrapError("Python entry file does not exist: " + str(entry))
    config = load_config(Path.cwd())
    return launch(entry, args, config, discover_assets(config))


def run(main: Entry) -> None:
    """Run a file's async entrypoint in an owned local Python-on-BEAM VM.

    The file is loaded again inside the VM. Existing Python objects and closures
    are not transferred from the calling interpreter.
    """
    if not inspect.iscoroutinefunction(main):
        raise TypeError("pybeam.run expects an async function, not a coroutine object")
    if register(main):
        return
    try:
        status = run_file(Path(sys.argv[0]), sys.argv[1:])
    except BootstrapError as error:
        print("PyBeam: " + str(error), file=sys.stderr)
        raise SystemExit(error.exit_status) from error
    if status:
        raise SystemExit(status)
