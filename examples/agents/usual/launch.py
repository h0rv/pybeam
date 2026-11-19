import argparse
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = Path(__file__).with_name("compose.yaml")
URL = "http://127.0.0.1:8813"


def compose(*arguments: str, project: str = "agents-usual") -> str:
    result = subprocess.run(  # noqa: S603
        ["docker", "compose", "-p", project, "-f", str(COMPOSE), *arguments],  # noqa: S607
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
        timeout=240,
    )
    return result.stdout


@contextmanager
def running(*, project: str = "agents-usual") -> Generator[None]:
    try:
        compose("up", "--build", "-d", project=project)
        with httpx.Client(timeout=1) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    if client.get(URL + "/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("Conventional app did not become ready")
                time.sleep(0.1)
        yield
    finally:
        compose("down", "--volumes", "--remove-orphans", project=project)


class OwningWorker(BaseModel):
    node: Literal["worker-1", "worker-2"]


class Ownership(BaseModel):
    snapshot: OwningWorker


def kill(session: UUID, *, project: str = "agents-usual") -> str:
    response = httpx.get(URL + "/api/sessions/" + session.hex, timeout=5)
    response.raise_for_status()
    worker = Ownership.model_validate_json(response.content).snapshot.node
    container = compose("ps", "-q", worker, project=project).strip()
    if not container:
        raise RuntimeError("Owning worker is not running: " + worker)
    subprocess.run(["docker", "kill", container], check=True, timeout=10)  # noqa: S603, S607
    return worker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["kill"])
    parser.add_argument("session", type=UUID)
    args = parser.parse_args()
    print("Killed " + kill(args.session))


if __name__ == "__main__":
    main()
