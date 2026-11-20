import argparse
import contextlib
import json
import os
import subprocess
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil
from failure_workload import counts

HERE = Path(__file__).resolve().parent
type Framework = Literal["pybeam", "celery", "ray"]


def running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def kill_tree(process: subprocess.Popen[bytes]) -> dict[str, int]:
    if process.poll() is not None:
        return {"killed_processes": 0, "live_after_kill": 0}
    root = psutil.Process(process.pid)
    root.suspend()
    tree = [root, *root.children(recursive=True)]
    for child in tree[1:]:
        try:
            child.suspend()
        except psutil.NoSuchProcess:
            continue
    for child in reversed(tree):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
    process.wait(timeout=10)
    deadline = time.monotonic() + 3
    while (live := sum(running(child) for child in tree)) and time.monotonic() < deadline:
        time.sleep(0.01)
    if live:
        raise RuntimeError("Captured process tree survived SIGKILL")
    return {"killed_processes": len(tree), "live_after_kill": live}


@contextlib.contextmanager
def launch(
    command: list[str], directory: Path, log_name: str, environment: dict[str, str]
) -> Generator[subprocess.Popen[bytes]]:
    with (directory / log_name).open("wb") as output:
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                kill_tree(process)


def require_alive(process: subprocess.Popen[bytes], deadline: float) -> None:
    if process.poll() is not None:
        raise RuntimeError("Worker exited early; inspect its log")
    if time.monotonic() >= deadline:
        raise TimeoutError("Worker progress deadline exceeded")


def run(framework: Framework, directory: Path, envs: Path, trial: int) -> dict[str, object]:
    directory.mkdir()
    markers = directory / "markers"
    markers.mkdir()
    python = str(envs / (framework + "314") / "bin/python")
    environment = dict(os.environ, PYTHONPATH=str(HERE), PROBE_QUEUE=directory.name)
    environment.pop("PYTHONHOME", None)
    environment.pop("PROBE_ROOT_PID", None)
    if framework == "celery":
        worker = [
            python,
            "-m",
            "celery",
            "-A",
            "celery_app:app",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--prefetch-multiplier=1",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
        ]
    else:
        worker = [python, str(HERE / ("durable_" + framework + ".py"))]
    command = worker if framework == "celery" else [*worker, "submit"]
    started = time.monotonic()
    with launch(command, directory, "initial.log", environment) as process:
        if framework == "celery":
            subprocess.run(  # noqa: S603
                [python, str(HERE / "celery_app.py"), "submit"],
                cwd=directory,
                env=environment,
                check=True,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        while not (directory / "submitted").exists() or counts(markers)["missing"] > 15:
            require_alive(process, started + 40)
            time.sleep(0.01)
        killed_at = time.monotonic() - started
        killed = kill_tree(process)
    before = counts(markers)
    if framework != "celery":
        (directory / "ready").unlink(missing_ok=True)
    restarted = time.monotonic()
    command = worker if framework == "celery" else [*worker, "recover"]
    with launch(command, directory, "recovered.log", environment) as process:
        deadline = restarted + 45
        if framework != "celery":
            while not (directory / "ready").exists():
                require_alive(process, deadline)
                time.sleep(0.01)
        if framework == "ray":
            until = time.monotonic() + 12
            while time.monotonic() < until:
                require_alive(process, deadline)
                time.sleep(0.05)
        else:
            while counts(markers)["missing"]:
                require_alive(process, deadline)
                time.sleep(0.02)
            time.sleep(2)
        after = counts(markers)
        recovery = time.monotonic() - restarted
    infra = {
        "pybeam": "managed launcher + guardian + BEAM/Mnesia; persistent local data dir; psutil",
        "celery": "prefork worker + 1 child; surviving Docker Redis 7 broker; celery[redis], psutil",
        "ray": "driver + local Ray cluster + task worker; psutil; no external queue",
    }
    return {
        "probe": "durable_sigkill",
        "framework": framework,
        "trial": trial,
        "submitted": 20,
        "concurrency": 1,
        "task_sleep_s": 0.5,
        "kill_after_s": killed_at,
        "before_restart": before,
        "after_restart": after,
        "recovery_observation_s": recovery,
        **killed,
        "lease_s": 2 if framework == "pybeam" else None,
        "visibility_timeout_s": 2 if framework == "celery" else None,
        "restore_sweep_s": 1 if framework == "celery" else None,
        "resubmitted": 0,
        "infra": infra[framework],
        "data_dir": str(directory / ".pybeam/data") if framework == "pybeam" else None,
    }


@dataclass
class Arguments(argparse.Namespace):
    framework: str = ""
    directory: Path = Path()
    envs: Path = Path.home() / "probe-env"
    trial: int = 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("framework", choices=("pybeam", "celery", "ray"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--envs", type=Path, default=Path.home() / "probe-env")
    parser.add_argument("--trial", type=int, default=1)
    args = parser.parse_args(namespace=Arguments())
    framework: str = args.framework
    if framework not in ("pybeam", "celery", "ray"):
        raise ValueError("Unknown framework")
    print(json.dumps(run(framework, args.directory, args.envs, args.trial)), flush=True)
