import argparse
import sys
from pathlib import Path

from pybeam.runtime.config import BootstrapError
from pybeam.runtime.entrypoint import run_file


def main() -> None:
    parser = argparse.ArgumentParser(prog="pybeam")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run an async Python entry file in a managed VM")
    run.add_argument("file", type=Path)
    run.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    path: object = options.file
    arguments: object = options.args
    if not isinstance(path, Path) or not isinstance(arguments, list):
        parser.error("Expected a Python file and its arguments")
    args: list[str] = []
    for argument in arguments:
        if not isinstance(argument, str):
            parser.error("Expected text arguments")
        args.append(argument)
    try:
        status = run_file(path, args)
    except BootstrapError as error:
        print("PyBeam: " + str(error), file=sys.stderr)
        raise SystemExit(error.exit_status) from error
    raise SystemExit(status)
