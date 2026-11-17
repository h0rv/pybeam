import argparse
import code
import codeop
import sys
from pathlib import Path
from typing import override

from pybeam.runtime.assets import discover_assets
from pybeam.runtime.attach import App, Connection, connect, running
from pybeam.runtime.config import BootstrapError, load_config
from pybeam.runtime.entrypoint import run_file

MENU = "[s] stacks and tasks  [p] Python shell  [q] quit"


class _Console(code.InteractiveConsole):
    def __init__(self, connection: Connection) -> None:
        super().__init__()
        self.connection = connection

    @override
    def runsource(self, source: str, filename: str = "<input>", symbol: str = "single") -> bool:
        try:
            if codeop.compile_command(source, filename, symbol) is None:
                return True
        except SyntaxError, OverflowError, ValueError:
            self.showsyntaxerror(filename)
            return False
        result = self.connection.request("evaluate", source)
        if result:
            self.write(result if result.endswith("\n") else result + "\n")
        return False


def _ps() -> None:
    apps = running()
    if not apps:
        print("No debuggable apps. Start one with: pybeam run --debug app.py")
        return
    print("{:<24} {:>8}  {}".format("NAME", "PID", "ENTRY"))
    for app in apps:
        print("{:<24} {:>8}  {}".format(app.name, app.pid, app.entry))


def _choose(name: str | None) -> App:
    apps = running()
    if name is not None:
        for app in apps:
            if app.name == name:
                return app
        raise BootstrapError("No debuggable app named " + name)
    if not apps:
        raise BootstrapError("No debuggable apps. Start one with: pybeam run --debug app.py")
    if len(apps) == 1:
        return apps[0]
    for index, app in enumerate(apps, 1):
        print("{}. {}  {}".format(index, app.name, app.entry))
    choice = input("App number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(apps):
        raise BootstrapError("Unknown app number: " + choice)
    return apps[int(choice) - 1]


def _debug(name: str | None) -> None:
    app = _choose(name)
    assets = discover_assets(load_config(Path.cwd()))
    with connect(app, assets) as connection:
        print("Attached to " + app.name + " (" + app.entry + ")")
        while True:
            try:
                command = input(MENU + "\n> ").strip()
            except EOFError:
                return
            match command:
                case "s":
                    print(connection.request("snapshot", ""), end="")
                case "p":
                    _Console(connection).interact(
                        banner="Python in " + app.name + ". Ctrl-D returns to the menu.",
                        exitmsg="",
                    )
                case "q":
                    return
                case _:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="pybeam")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run an async Python entry file in a managed VM")
    run.add_argument("--debug", action="store_true", help="Allow pybeam debug to attach")
    run.add_argument("file", type=Path)
    run.add_argument("args", nargs=argparse.REMAINDER)
    commands.add_parser("ps", help="List apps started with --debug")
    attach = commands.add_parser("debug", help="Inspect a running app started with --debug")
    attach.add_argument("name", nargs="?")
    options = parser.parse_args()
    command: object = options.command
    try:
        match command:
            case "ps":
                _ps()
                return
            case "debug":
                name: object = options.name
                if name is not None and not isinstance(name, str):
                    parser.error("Expected an app name")
                _debug(name)
                return
            case _:
                pass
        path: object = options.file
        arguments: object = options.args
        debug: object = options.debug
        if not isinstance(path, Path) or not isinstance(arguments, list):
            parser.error("Expected a Python file and its arguments")
        args: list[str] = []
        for argument in arguments:
            if not isinstance(argument, str):
                parser.error("Expected text arguments")
            args.append(argument)
        status = run_file(path, args, debug=debug is True)
    except BootstrapError as error:
        print("PyBEAM: " + str(error), file=sys.stderr)
        raise SystemExit(error.exit_status) from error
    raise SystemExit(status)
