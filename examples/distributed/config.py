import os
from pathlib import Path

from pybeam.distribution import Distribution, PythonNode
from pybeam.runtime.config import Config

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def configuration() -> tuple[Config, Distribution]:
    runtime = Path(
        os.environ.get(
            "PYBEAM_DEVELOPMENT_RUNTIME",
            str(ROOT.parent / "native-local-distribution-admission" / "_build" / "default"),
        )
    )
    return Config(development_runtime=runtime), Distribution(
        nodes=(
            PythonNode(alias="a", entry=HERE / "node_a.py", port=45701),
            PythonNode(alias="b", entry=HERE / "node_b.py", port=45702),
        ),
        discovery_port=45700,
        capacity=2,
        max_bytes=4096,
    )
