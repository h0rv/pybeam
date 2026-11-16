import os
import subprocess
import sys
from pathlib import Path
from typing import assert_type

import pytest
from pydantic import ValidationError

from pybeam.distribution import Reply, Request, RequestTooLarge, remote, reservation
from pybeam.runtime.cluster import LocalCluster
from pybeam.runtime.config import Config, Distribution, PythonNode, parse_config
from tests.typechecking import TYPECHECK_CONFIG


def test_configuration__strict_opt_in_and_round_trip(tmp_path: Path) -> None:
    entry = tmp_path / "entry.py"
    entry.write_text("raise RuntimeError('must not execute on import')\n")
    assert Config().distribution is False
    with pytest.raises(ValidationError):
        Config.model_validate({"distribution": True})
    config = parse_config(
        {
            "distribution": {
                "nodes": [
                    {"alias": "a", "entry": "entry.py", "port": 45701},
                    {"alias": "b", "entry": "entry.py", "port": 45702},
                ],
                "discovery_port": 45700,
                "capacity": 2,
                "max_bytes": 4096,
            }
        },
        tmp_path,
    )
    assert isinstance(config.distribution, Distribution)
    assert config.distribution.nodes[0].entry == entry
    assert Config.model_validate_json(config.model_dump_json()) == config
    config = Config(development_runtime=tmp_path)
    assert Config.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    "patch",
    [
        {"discovery_port": 4369},
        {"discovery_port": 8642},
        {"discovery_port": 45701},
        {"capacity": 0},
        {"capacity": 9},
        {"max_bytes": 16385},
        {"max_bytes": "4096"},
    ],
)
def test_distribution__bounded_private_configuration(
    tmp_path: Path, patch: dict[str, object]
) -> None:
    entry = tmp_path / "entry.py"
    entry.touch()
    base: dict[str, object] = {
        "nodes": (
            PythonNode(alias="a", entry=entry, port=45701),
            PythonNode(alias="b", entry=entry, port=45702),
        ),
        "discovery_port": 45700,
        "capacity": 2,
        "max_bytes": 4096,
    }
    with pytest.raises(ValidationError):
        Distribution.model_validate(base | patch)


def test_data_boundary__encoded_size_and_malformed_replies() -> None:
    claim = reservation("A" * 32, "id", "é")
    assert claim.size == 2
    with pytest.raises(ValidationError):
        reservation("A" * 32, "id", "x" * 4097)
    with pytest.raises(ValidationError):
        Reply.model_validate_json('{"status":"pretend_success"}')
    with pytest.raises(ValidationError):
        Request(op="submit", payload="x" * 4097)
    with pytest.raises(RequestTooLarge):
        Request(op="submit", payload="\u6f22" * 4096).encode()


def test_distribution__lazy_plain_cpython_import() -> None:
    code = "import sys; import pybeam.distribution; import pybeam.runtime.cluster; "
    code += "assert not {'erlang', 'litestar', 'fastapi', 'hornbeam', 'pydantic_ai'} & sys.modules.keys()"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_cluster__lifecycle_controls_cannot_write_before_ownership(tmp_path: Path) -> None:
    entry = tmp_path / "entry.py"
    entry.touch()
    distribution = Distribution(
        nodes=(
            PythonNode(alias="a", entry=entry, port=45701),
            PythonNode(alias="b", entry=entry, port=45702),
        ),
        discovery_port=45700,
        capacity=2,
        max_bytes=4096,
    )
    cluster = LocalCluster(distribution)
    with pytest.raises(RuntimeError):
        cluster.restart("a")
    with pytest.raises(RuntimeError):
        cluster.stop("a")
    with pytest.raises(RuntimeError):
        _ = cluster.directory
    cluster.close()
    cluster.close()
    with pytest.raises(RuntimeError):
        cluster.__enter__()
    assert tuple(tmp_path.iterdir()) == (entry,)


async def positive(cluster: LocalCluster) -> None:
    assert_type(cluster.restart("b").pid, int)
    assert_type(await remote("b", Request(op="snapshot")), Reply)


def test_distribution__negative_public_types(tmp_path: Path) -> None:
    source = tmp_path / "negative.py"
    source.write_text(
        "from pybeam.runtime.config import PythonNode\n"
        "from pybeam.runtime.cluster import LocalCluster\n"
        "from pybeam.distribution import Request, remote\n"
        "from pathlib import Path\n"
        "PythonNode(alias='unknown', entry=Path('x'), port=45701)\n"
        "LocalCluster(True)\n"
        "async def bad() -> None:\n"
        "    await remote('unconfigured@host', Request(op='snapshot'))\n"
    )
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(root / ".venv/bin/ty"), "check", *TYPECHECK_CONFIG, "--error-on-warning", str(source)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout.count("invalid-argument-type") >= 3
