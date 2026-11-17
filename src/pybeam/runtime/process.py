import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from pybeam.runtime.assets import RuntimeAssets
from pybeam.runtime.attach import EPMD, LOOPBACK, App, announce, node_name
from pybeam.runtime.config import BootstrapError, CleanupError, Config


@dataclass(slots=True)
class _Shutdown:
    received: int = 0
    deadline: float | None = None


def _send(process: subprocess.Popen[bytes], number: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, number)


def _stop_owned(process: subprocess.Popen[bytes], timeout: float) -> None:
    # Popen(start_new_session=True) makes this group exclusively ours.
    _send(process, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _send(process, signal.SIGKILL)
        process.wait(timeout=1.0)
    finally:
        # Also reap descendants left behind by an exited VM leader.
        _send(process, signal.SIGKILL)


def _completion(path: Path) -> int | None:
    try:
        value = int(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise CleanupError("Cannot read managed entry completion status", 1) from error
    if not 0 <= value <= 255:
        raise CleanupError("Invalid managed entry completion status", 1)
    return value


def _command(assets: RuntimeAssets, *, python_paths: tuple[str, ...] = ()) -> list[str]:
    paths = (
        "["
        + ",".join(
            "<<" + ",".join(str(byte) for byte in os.fsencode(path)) + ">>" for path in python_paths
        )
        + "]"
    )
    return [
        str(assets.executable),
        "+S",
        "1:1",
        "+Bi",
        "-noshell",
        "-noinput",
        *(["-boot", str(assets.boot)] if assets.boot is not None else []),
        "-pa",
        *(str(path) for path in assets.code_paths),
        *(["-erlang_python", "paths", paths] if python_paths else []),
        "-s",
        "py_launch",
        "start",
    ]


def launch(
    entry: Path,
    args: list[str],
    config: Config,
    assets: RuntimeAssets,
    *,
    options: tuple[str, ...] = (),
    extra_environment: dict[str, str] | None = None,
    debug: bool = False,
) -> int:
    if config.distribution is not False:
        raise BootstrapError("Configured distribution requires the explicit LocalCluster launcher")
    if os.name != "posix":
        raise BootstrapError("The managed bootstrap currently requires POSIX process groups")
    if threading.current_thread() is not threading.main_thread():
        raise BootstrapError("pybeam.run must launch from the main Python thread")
    with tempfile.TemporaryDirectory(prefix="pybeam-") as temporary:
        ready = Path(temporary) / "ready"
        shutdown = Path(temporary) / "shutdown"
        completed = Path(temporary) / "completed"
        cleanup = Path(temporary) / "cleanup"
        environment = dict(os.environ)
        environment.update(assets.environment)
        if extra_environment is not None:
            environment.update(extra_environment)
        else:
            for key in (
                "PYBEAM_DISTRIBUTION",
                "PYBEAM_OWNER_DIRECTORY",
                "PYBEAM_GUARDIAN_IDENTITY",
                "PYBEAM_OUTER_GUARDIAN",
                "PYBEAM_CLUSTER_DIRECTORY",
                "PYBEAM_CLUSTER_READY",
                "PYBEAM_NODE_ALIAS",
            ):
                environment.pop(key, None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYBEAM_SHUTDOWN"] = str(shutdown)
        environment["PYBEAM_COMPLETION"] = str(completed)
        environment["PYBEAM_CLEANUP"] = str(cleanup)
        environment["PYBEAM_SHUTDOWN_TIMEOUT_MS"] = str(math.ceil(config.shutdown_timeout * 1000))
        python_paths = tuple(
            dict.fromkeys((str(entry.parent), *(str(Path(path).resolve()) for path in sys.path)))
        )
        environment["PYBEAM_BOOTSTRAP"] = json.dumps(
            {
                "argv": [str(entry), *args],
                "python_paths": python_paths,
                "ready": str(ready),
                "completed": str(completed),
            },
            ensure_ascii=True,
        )
        name = node_name(entry)
        if debug:
            environment.update(EPMD)
            options = (*options, "-name", name + "@127.0.0.1", *LOOPBACK)
        record: Path | None = None
        guardian = environment.get("PYBEAM_GUARDIAN_IDENTITY")
        state = _Shutdown()
        completion_status: int | None = None
        announced = False
        startup_deadline = time.monotonic() + config.startup_timeout
        process: subprocess.Popen[bytes] | None = None

        def forward(number: int, _frame: FrameType | None) -> None:
            if not state.received:
                state.received = number
                if state.deadline is None:
                    state.deadline = time.monotonic() + config.shutdown_timeout
                shutdown.write_text(str(number))
            if process is not None:
                _send(process, number)

        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGINT, forward)
            signal.signal(signal.SIGTERM, forward)
            try:
                command = [*_command(assets, python_paths=python_paths), *options]
                if "PYBEAM_OWNER_DIRECTORY" in environment:
                    command = [sys.executable, "-m", "pybeam.runtime.guardian", "gate", *command]
                process = subprocess.Popen(  # noqa: S603
                    command,
                    env=environment,
                    start_new_session=True,
                )
            except OSError as error:
                raise BootstrapError("Cannot start the managed runtime: " + str(error)) from error
            if state.received:
                _send(process, state.received)
            if debug:
                record = announce(
                    App(name, name + "@127.0.0.1", process.pid, str(entry), str(Path.cwd()))
                )
            while True:
                if guardian is not None:
                    from pybeam.runtime.ownership import IDENTITY, resolve

                    if resolve(IDENTITY.decode(guardian)) is None:
                        raise BootstrapError(
                            "Guardian exited; managed execution ownership is uncertain"
                        )
                if completion_status is None:
                    completion_status = _completion(completed)
                    if completion_status is not None and state.deadline is None:
                        state.deadline = time.monotonic() + config.shutdown_timeout
                status = process.poll()
                if status is not None:
                    if completion_status is None:
                        completion_status = _completion(completed)
                    exit_status = status if status >= 0 else 128 - status
                    entry_status = completion_status or exit_status
                    if state.received:
                        entry_status = 128 + state.received
                    if not ready.exists() and completion_status is None and status == 0:
                        raise BootstrapError(
                            "Managed runtime exited before registering the entrypoint"
                        )
                    if ready.exists() or completion_status is not None or state.received:
                        try:
                            acknowledged = cleanup.read_text() == "clean"
                        except FileNotFoundError:
                            acknowledged = False
                        except OSError as error:
                            raise CleanupError(
                                "Cannot read managed cleanup acknowledgment", entry_status
                            ) from error
                        if completion_status is None or not acknowledged:
                            raise CleanupError(
                                "Managed runtime cleanup was not acknowledged "
                                + "(entry status {}, VM exit status {})".format(
                                    entry_status, exit_status
                                ),
                                entry_status,
                            )
                        if not state.received and exit_status != completion_status:
                            raise CleanupError(
                                "Managed VM exit disagrees with entry status", entry_status
                            )
                    return entry_status
                if not announced and ready.exists() and "PYBEAM_CLUSTER_READY" in environment:
                    from pybeam.runtime.ownership import IDENTITY, durable_write, identity

                    durable_write(
                        Path(environment["PYBEAM_CLUSTER_READY"]),
                        IDENTITY.encode(identity(process.pid)).decode(),
                    )
                    announced = True
                now = time.monotonic()
                if state.deadline is not None and now >= state.deadline:
                    entry_status = (
                        128 + state.received if state.received else (completion_status or 0)
                    )
                    raise CleanupError(
                        "Managed runtime shutdown deadline exceeded (entry status {}); "
                        "VM exit and acknowledged cleanup were not both observed".format(
                            entry_status
                        ),
                        entry_status,
                    )
                if not ready.exists() and state.deadline is None and now >= startup_deadline:
                    raise BootstrapError("Managed runtime startup deadline exceeded")
                time.sleep(0.02)
        finally:
            try:
                if process is not None:
                    grace = config.shutdown_timeout
                    if state.deadline is not None:
                        grace = max(0.0, state.deadline - time.monotonic())
                    _stop_owned(process, grace)
            finally:
                if record is not None:
                    record.unlink(missing_ok=True)
                signal.signal(signal.SIGINT, previous_int)
                signal.signal(signal.SIGTERM, previous_term)
