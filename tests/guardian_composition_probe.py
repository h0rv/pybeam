import asyncio
import os
import sys
import time
from pathlib import Path

import psutil

from pybeam import run
from pybeam.runtime.cluster import LocalCluster
from pybeam.runtime.config import Distribution, PythonNode
from pybeam.runtime.guardian import GuardianEndpoint
from pybeam.runtime.ownership import Registry, durable_write, identity


def node() -> None:
    endpoint = GuardianEndpoint.model_validate_json(os.environ["PYBEAM_OUTER_GUARDIAN"])
    birth = identity(psutil.Process())
    for directory in (endpoint.directory, Path(os.environ["PYBEAM_OWNER_DIRECTORY"])):
        registry = Registry.model_validate_json((directory / "ownership.json").read_text())
        if birth not in registry.owned:
            raise RuntimeError("Entry without acknowledged birth ownership")
    durable_write(
        endpoint.directory / (os.environ["PYBEAM_NODE_ALIAS"] + ".entry.json"),
        birth.model_dump_json(),
    )
    run(idle)


async def idle() -> None:
    await asyncio.sleep(60)


def adapter(directory: Path) -> None:
    endpoint = GuardianEndpoint.model_validate_json((directory / "guardian.json").read_text())
    entry = Path(__file__).resolve()
    distribution = Distribution(
        nodes=(
            PythonNode(alias="a", entry=entry, port=45701),
            PythonNode(alias="b", entry=entry, port=45702),
        ),
        discovery_port=45700,
        capacity=1,
        max_bytes=128,
    )
    cluster = LocalCluster(distribution, guardian=endpoint)
    durable_write(directory / "adapter.birth.json", identity(psutil.Process()).model_dump_json())
    with cluster:
        guard = cluster._guardian_identity
        if guard is None:
            raise RuntimeError("No inner guardian birth")
        durable_write(directory / "inner.birth.json", guard.model_dump_json())
        durable_write(
            directory / "nested.json",
            Registry(phase="tracking", owned=tuple(cluster.identities)).model_dump_json(),
        )
        deadline = time.monotonic() + 12
        while not (directory / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Composition control not released")
            time.sleep(0.01)
    if cluster.cleanup is None or cluster.cleanup.phase != "confirmed":
        raise RuntimeError("Inner cleanup unconfirmed")
    durable_write(directory / "inner-cleanup.json", cluster.cleanup.model_dump_json())


if __name__ == "__main__":
    if "PYBEAM_NODE_ALIAS" in os.environ:
        node()
    else:
        adapter(Path(sys.argv[1]))
