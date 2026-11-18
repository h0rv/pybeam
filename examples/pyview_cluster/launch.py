import os
import secrets
import signal
import site
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT.parent / "hornbeam" / "examples" / "run_asgi.escript"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address: object = listener.getsockname()
        if isinstance(address, tuple) and len(address) == 2:
            port: object = address[1]
            if isinstance(port, int):
                return port
        raise RuntimeError("Expected an IPv4 socket address")


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("Feed process required forced shutdown") from None


@contextmanager
def running(
    *,
    runtime: Path = ROOT / "_build" / "hornbeam",
    ports: tuple[int, int] = (8771, 8772),
    output: BinaryIO | None = None,
) -> Generator[tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]]]:
    epmd_port = free_port()
    environment = dict(os.environ, ERL_EPMD_PORT=str(epmd_port), ERL_EPMD_ADDRESS="127.0.0.1")
    for key in ("ERL_FLAGS", "ERL_AFLAGS", "ERL_ZFLAGS"):
        environment.pop(key, None)
    cookie = secrets.token_hex(32)
    epmd = subprocess.Popen(  # noqa: S603
        ["epmd", "-port", str(epmd_port), "-address", "127.0.0.1"],  # noqa: S607
        env=environment,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    processes: list[subprocess.Popen[bytes]] = []
    try:
        deadline = time.monotonic() + 5
        while True:
            if epmd.poll() is not None:
                raise RuntimeError("Private EPMD exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", epmd_port), timeout=0.1) as probe:
                    probe.sendall(b"\x00\x01n")
                    probe.recv(4)
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Private EPMD did not start") from None
                time.sleep(0.02)
        for alias, peer, port in (("a", "b", ports[0]), ("b", "a", ports[1])):
            flags = (
                "-sname pybeam_feed_"
                + alias
                + "@localhost -setcookie "
                + cookie
                + " -start_epmd false -kernel inet_dist_use_interface '{127,0,0,1}'"
                + " sync_nodes_optional \"['pybeam_feed_"
                + peer
                + "@localhost']\" sync_nodes_timeout 5000"
            )
            processes.append(
                subprocess.Popen(  # noqa: S603
                    [  # noqa: S607
                        "escript",
                        str(LAUNCHER),
                        str(runtime),
                        "examples.pyview_cluster.main:app",
                        "127.0.0.1:" + str(port),
                        "on",
                        str(ROOT),
                        str(ROOT / "src"),
                        *site.getsitepackages(),
                    ],
                    cwd=ROOT,
                    env=dict(environment, ERL_FLAGS=flags, PYBEAM_FEED_NODE=alias.upper()),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        yield processes[0], processes[1]
    finally:
        try:
            with ExitStack() as cleanup:
                for process in processes:
                    cleanup.callback(stop, process)
        finally:
            stop(epmd)


class Stopped(Exception):
    pass


def interrupted(_number: int, _frame: FrameType | None) -> None:
    raise Stopped


def main() -> None:
    previous = {
        number: signal.signal(number, interrupted) for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with running() as processes:
            print("Open http://127.0.0.1:8771/ and http://127.0.0.1:8772/", flush=True)
            print("Press Ctrl+C to stop both nodes.", flush=True)
            while all(process.poll() is None for process in processes):
                time.sleep(0.1)
            raise RuntimeError("A feed node exited unexpectedly")
    except Stopped:
        print("Both feed nodes stopped.", flush=True)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    main()
