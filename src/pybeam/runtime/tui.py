import curses
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pybeam.codec import CodecError
from pybeam.runtime.assets import discover_assets
from pybeam.runtime.attach import App, Connection, connect, running
from pybeam.runtime.config import BootstrapError, load_config
from pybeam.runtime.console import Console
from pybeam.runtime.inspector import REPORT, Report, Resource

REFRESH = 2.0
ENTER = (10, 13, curses.KEY_ENTER)
BACK = (27, ord("b"), curses.KEY_LEFT)


def _clean(text: str) -> str:
    return "".join(char if char.isprintable() else " " for char in text)


def _put(screen: curses.window, row: int, text: str, *, selected: bool = False) -> None:
    height, width = screen.getmaxyx()
    if 0 <= row < height and width > 1:
        with suppress(curses.error):
            screen.addnstr(row, 0, _clean(text), width - 1, curses.A_REVERSE if selected else 0)


def _frame(screen: curses.window, title: str, status: str, help_text: str) -> int:
    screen.erase()
    height, width = screen.getmaxyx()
    _put(screen, 0, "pybeam / " + title, selected=True)
    if height < 8 or width < 40:
        _put(screen, 2, "Resize terminal (40 columns, 8 rows)")
        _put(screen, height - 1, "q quit")
        return 0
    _put(screen, 1, status)
    _put(screen, height - 1, help_text)
    return height - 4


@dataclass(slots=True)
class Selection:
    index: int = 0

    def move(self, key: int, count: int) -> None:
        if key in (ord("j"), curses.KEY_DOWN):
            self.index += 1
        elif key in (ord("k"), curses.KEY_UP):
            self.index -= 1
        elif key == curses.KEY_HOME:
            self.index = 0
        elif key == curses.KEY_END:
            self.index = count - 1
        self.index = max(0, min(self.index, count - 1))


def _summary(resource: Resource) -> str:
    return "  ".join(stat.name + " " + stat.value for stat in resource.stats[:3])


def _shell(screen: curses.window, connection: Connection) -> None:
    curses.def_prog_mode()
    curses.endwin()
    try:
        Console(connection).interact(
            banner="Live Python globals. Statements can change the app. Ctrl-D returns.",
            exitmsg="",
        )
    finally:
        curses.reset_prog_mode()
        screen.clear()
        screen.refresh()


class _Quit(Exception):
    pass


def _stacks(screen: curses.window, connection: Connection, resource: Resource | None) -> None:
    selection = Selection()
    lines: list[str] = []
    refresh = True
    while True:
        if refresh:
            _frame(screen, "Python stacks", "Reading Python stacks...", "")
            screen.refresh()
            text = connection.request("snapshot", "", timeout=3.0)
            lines = ["All worker threads and entry-loop tasks (not just the selected resource)."]
            if resource is not None:
                lines.extend(["", resource.name + " / " + resource.state])
                lines.extend(stat.name + ": " + stat.value for stat in resource.stats)
            lines.extend(["", "Own-GIL and isolated interpreter stacks are not visible here.", ""])
            lines.extend(text.splitlines() or ["No Python stacks available."])
            refresh = False
        title = "Python stacks" if resource is None else resource.name
        size = _frame(screen, title, "Snapshot; r reads again", "up/down scroll  r refresh  p shell  b back  q quit")
        selection.index = min(selection.index, max(0, len(lines) - max(1, size)))
        for row, line in enumerate(lines[selection.index : selection.index + size], 3):
            _put(screen, row, line)
        screen.refresh()
        key = screen.getch()
        if key in BACK:
            return
        if key == ord("q"):
            raise _Quit
        if key == ord("r"):
            refresh = True
        if key == ord("p"):
            _shell(screen, connection)
        selection.move(key, len(lines))


def _dashboard(screen: curses.window, app: App, connection: Connection) -> None:
    selection = Selection()
    report = Report((), ())
    deadline = 0.0
    while True:
        if time.monotonic() >= deadline:
            previous = report.resources[selection.index].identity if report.resources else None
            report = REPORT.decode(connection.request("inspect", "", timeout=3.0))
            resources = tuple(sorted(report.resources, key=lambda row: (row.kind, row.name, row.identity)))
            report = Report(resources, report.unavailable)
            selection.index = next(
                (index for index, row in enumerate(resources) if row.identity == previous), 0
            )
            deadline = time.monotonic() + REFRESH
        status = "Attached / refresh 2s / PID " + str(app.pid)
        if report.unavailable:
            status = "Degraded / unavailable: " + ", ".join(report.unavailable)
        size = _frame(screen, app.name, status, "up/down select  Enter stacks  p shell  r refresh  b apps  q quit")
        if not report.resources:
            _put(screen, 3, "No actors, task supervisors or opened queues.")
            _put(screen, 4, "s shows Python threads and entry-loop tasks; p opens the shell.")
        start = max(0, selection.index - size + 1)
        for index in range(start, min(len(report.resources), start + size)):
            resource = report.resources[index]
            line = "{:<6} {:<24} {:<10} {}".format(
                resource.kind, resource.name[:24], resource.state, _summary(resource)
            )
            _put(screen, 3 + index - start, line, selected=index == selection.index)
        screen.refresh()
        key = screen.getch()
        if key == ord("q"):
            raise _Quit
        if key in BACK:
            return
        if key in ENTER or key == ord("s"):
            resource = report.resources[selection.index] if report.resources else None
            _stacks(screen, connection, resource)
            deadline = 0.0
        if key == ord("p"):
            _shell(screen, connection)
            deadline = 0.0
        if key == ord("r"):
            deadline = 0.0
        selection.move(key, len(report.resources))


def _attach(screen: curses.window, app: App) -> None:
    _frame(screen, app.name, "Attaching...", "")
    screen.refresh()
    assets = discover_assets(load_config(Path(app.project)))
    with connect(app, assets) as connection:
        _dashboard(screen, app, connection)


def _main(screen: curses.window, name: str | None) -> None:
    screen.timeout(100)
    with suppress(curses.error):
        curses.curs_set(0)
    selection = Selection()
    apps: list[App] = []
    deadline = 0.0
    error = ""
    while True:
        if time.monotonic() >= deadline:
            previous = apps[selection.index].name if apps else None
            apps = running()
            selection.index = next((i for i, app in enumerate(apps) if app.name == previous), 0)
            deadline = time.monotonic() + REFRESH
        if name is not None:
            chosen = next((app for app in apps if app.name == name), None)
            if chosen is None:
                raise BootstrapError("No debuggable app named " + name)
            name = None
        else:
            chosen = None
        size = _frame(screen, "apps", error or "Running Python apps / refresh 2s", "up/down select  Enter attach  r refresh  q quit")
        if not apps:
            _put(screen, 3, "No debuggable apps.")
            _put(screen, 4, "Start one with: pybeam run --debug app.py")
        start = max(0, selection.index - size + 1)
        for index in range(start, min(len(apps), start + size)):
            app = apps[index]
            _put(screen, 3 + index - start, "{:<24} {:>8}  {}".format(app.name, app.pid, app.entry), selected=index == selection.index)
        screen.refresh()
        key = screen.getch() if chosen is None else -1
        if key == ord("q"):
            return
        if key == ord("r"):
            deadline = 0.0
        if key in ENTER and apps:
            chosen = apps[selection.index]
        if chosen is not None:
            try:
                _attach(screen, chosen)
                error = ""
            except BootstrapError, CodecError, OSError as failure:
                error = "Disconnected: " + str(failure)
            deadline = 0.0
        selection.move(key, len(apps))


def run(name: str | None) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BootstrapError("pybeam tui needs a terminal; use pybeam ps for text output")
    try:
        curses.wrapper(_main, name)
    except _Quit, KeyboardInterrupt:
        pass
    except curses.error as error:
        raise BootstrapError("Cannot use this terminal: " + str(error)) from error
