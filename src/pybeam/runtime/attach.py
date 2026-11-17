import base64
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from pybeam.runtime.assets import RuntimeAssets
from pybeam.runtime.config import BootstrapError

type Operation = Literal["snapshot", "evaluate"]

LOOPBACK = ["-kernel", "inet_dist_use_interface", "{127,0,0,1}"]
EPMD = {"ERL_EPMD_ADDRESS": "127.0.0.1"}

# Runs on a hidden client node. A process spawned on the target owns one
# worker context and stops it when this client goes away.
_CLIENT = """
Target = list_to_atom(os:getenv("PYBEAM_DEBUG_TARGET")),
true = net_kernel:connect_node(Target),
Client = self(),
Owner = spawn(Target, fun() ->
    erlang:monitor(process, Client),
    {ok, Ctx} = py_context:new(#{mode => worker}),
    Serve = fun Serve() ->
        receive
            {request, Op, Arg} ->
                Client ! {reply, py_context:call(Ctx, <<"pybeam.runtime.debug">>, Op, [Arg])},
                Serve();
            {'DOWN', _, process, Client, _} ->
                py_context:stop(Ctx)
        end
    end,
    Serve()
end),
Loop = fun Loop() ->
    case io:get_line("") of
        eof -> halt(0);
        Line ->
            [Op, Arg] = string:split(string:trim(Line, trailing, "\\n"), " "),
            Owner ! {request, list_to_atom(Op), base64:decode(Arg)},
            Text = receive
                {reply, {ok, Value}} when is_binary(Value) -> Value;
                {reply, Other} -> unicode:characters_to_binary(io_lib:format("~p~n", [Other]))
            end,
            io:put_chars([base64:encode(Text), $\\n]),
            Loop()
    end
end,
Loop().
"""


@dataclass(frozen=True, slots=True)
class App:
    name: str
    node: str
    pid: int
    entry: str


def registry() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = Path(base) / ("pybeam-" + str(os.getuid()))
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def node_name(entry: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", entry.stem) or "app"
    return stem + "-" + str(os.getpid())


def announce(app: App) -> Path:
    path = registry() / (app.name + ".json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(asdict(app), file)
    return path


def _parse(path: Path) -> App | None:
    try:
        data: object = json.loads(path.read_text())
    except OSError, ValueError:
        return None
    match data:
        case {"name": str(name), "node": str(node), "pid": int(pid), "entry": str(entry)}:
            return App(name, node, pid, entry)
        case _:
            return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running() -> list[App]:
    """List debuggable apps started by this user, removing records of exited ones."""
    apps: list[App] = []
    for path in sorted(registry().glob("*.json")):
        app = _parse(path)
        if app is not None and _alive(app.pid):
            apps.append(app)
        else:
            with suppress(FileNotFoundError):
                path.unlink()
    return apps


@dataclass(slots=True)
class Connection:
    process: subprocess.Popen[bytes]

    def request(self, operation: Operation, payload: str) -> str:
        stdin, stdout = self.process.stdin, self.process.stdout
        if stdin is None or stdout is None:
            raise BootstrapError("Debug client has no pipes")
        stdin.write(operation.encode() + b" " + base64.b64encode(payload.encode()) + b"\n")
        stdin.flush()
        line = stdout.readline()
        if not line:
            raise BootstrapError("Debug connection closed")
        try:
            return base64.b64decode(line.strip(), validate=True).decode()
        except ValueError as error:
            raise BootstrapError("Debug client failed: " + line.decode().strip()) from error


@contextmanager
def connect(app: App, assets: RuntimeAssets) -> Generator[Connection]:
    """Attach a hidden loopback node that runs requests in a fresh worker context."""
    environment = dict(os.environ)
    environment.update(assets.environment)
    environment.update(EPMD)
    environment["PYBEAM_DEBUG_TARGET"] = app.node
    command = [
        str(assets.executable),
        "-name",
        "pybeam-debug-" + str(os.getpid()) + "@127.0.0.1",
        "-hidden",
        "-noshell",
        *LOOPBACK,
        "-eval",
        _CLIENT,
    ]
    try:
        process = subprocess.Popen(  # noqa: S603
            command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
    except OSError as error:
        raise BootstrapError("Cannot start the debug client: " + str(error)) from error
    try:
        yield Connection(process)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
