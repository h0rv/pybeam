import os
import secrets
import signal
import site
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import BinaryIO
from uuid import uuid4

import httpx

from examples.agents.usual.launch import URL
from examples.agents.usual.launch import running as usual_running

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "_checkouts" / "hornbeam" / "examples" / "run_asgi.escript"
PORTS = (8810, 8811, 8812)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def stop(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


@contextmanager
def running(
    *,
    directory: Path,
    ports: tuple[int, int, int] = PORTS,
    output: BinaryIO | None = None,
) -> Generator[list[subprocess.Popen[bytes]]]:
    directory.mkdir(parents=True, exist_ok=True)
    epmd_port = free_port()
    environment = dict(os.environ, ERL_EPMD_PORT=str(epmd_port), ERL_EPMD_ADDRESS="127.0.0.1")
    for key in ("ERL_FLAGS", "ERL_AFLAGS", "ERL_ZFLAGS"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT), str(ROOT / "src"), *site.getsitepackages())
    )
    cookie = secrets.token_hex(32)
    with ExitStack() as cleanup:
        epmd = subprocess.Popen(  # noqa: S603
            ["epmd", "-port", str(epmd_port), "-address", "127.0.0.1"],  # noqa: S607
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        cleanup.callback(stop, epmd)
        deadline = time.monotonic() + 5
        while True:
            if epmd.poll() is not None:
                raise RuntimeError("Private EPMD exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", epmd_port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Private EPMD did not start") from None
                time.sleep(0.02)
        processes: list[subprocess.Popen[bytes]] = []
        for alias, port in zip("abc", ports, strict=True):
            cwd = directory / alias
            cwd.mkdir(exist_ok=True)
            (cwd / "pyproject.toml").write_text('[tool.pybeam]\ndata_dir = "state"\n')
            peers = ",".join("'agents_" + peer + "@localhost'" for peer in "abc" if peer != alias)
            python = "<<" + ",".join(str(byte) for byte in os.fsencode(sys.executable)) + ">>"
            flags = (
                "-sname agents_"
                + alias
                + "@localhost -setcookie "
                + cookie
                + " -start_epmd false -kernel inet_dist_use_interface '{127,0,0,1}'"
                + ' sync_nodes_optional "['
                + peers
                + ']" sync_nodes_timeout 5000'
                + " -erlang_python isolated_python '"
                + python
                + "'"
            )
            process = subprocess.Popen(  # noqa: S603
                [  # noqa: S607
                    "escript",
                    str(LAUNCHER),
                    str(ROOT / "_build" / "hornbeam"),
                    "examples.agents.app:app",
                    "127.0.0.1:" + str(port),
                    "on",
                    str(ROOT),
                    str(ROOT / "src"),
                    *site.getsitepackages(),
                ],
                cwd=cwd,
                env=dict(
                    environment,
                    ERL_FLAGS=flags,
                    PYBEAM_AGENTS_NODE=alias,
                    PYBEAM_AGENTS_AUDIT=str(directory / "audit"),
                ),
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            processes.append(process)
            cleanup.callback(stop, process)
        with httpx.Client(timeout=1) as client:
            for port in ports:
                deadline = time.monotonic() + 45
                while True:
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError(
                            "An agents node exited during startup; inspect its output"
                        )
                    try:
                        if (
                            client.get("http://127.0.0.1:" + str(port) + "/health").status_code
                            == 200
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Agents node did not become ready: " + str(port))
                    time.sleep(0.05)
        yield processes


class Stopped(Exception):
    pass


def interrupted(_number: int, _frame: FrameType | None) -> None:
    raise Stopped


def main() -> None:
    previous = {
        number: signal.signal(number, interrupted) for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with (
            usual_running(),
            running(directory=ROOT / "_build" / "agents" / uuid4().hex) as processes,
        ):
            for node, port in zip("abc", PORTS, strict=True):
                print("Node " + node + ": http://127.0.0.1:" + str(port) + "/", flush=True)
            print("FastAPI + Redis + Celery: " + URL + "/", flush=True)
            print(
                "Use Node failure on a node's page to SIGKILL it. Ctrl+C stops the cluster.",
                flush=True,
            )
            while any(process.poll() is None for process in processes):
                time.sleep(0.1)
    except Stopped:
        print("Agents cluster stopped.", flush=True)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    main()
