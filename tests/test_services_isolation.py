import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_services__no_web_dependency_or_standard_loop_fallback() -> None:
    code = """
import asyncio
import sys
sys.path[:0] = PATHS
from tests.native.service_helpers import load_service_helpers
load_service_helpers()
from pybeam.services.background import ServiceSlot

async def consume() -> None:
    raise AssertionError('must not run on a standard loop')

async def check() -> None:
    try:
        ServiceSlot(capacity=1).start(consume)
    except RuntimeError as error:
        assert str(error) == 'services require a running ErlangEventLoop'
    else:
        raise AssertionError('standard asyncio fallback must be rejected')

asyncio.run(check())
assert not {'hornbeam', 'fastapi', 'litestar', 'pyview', 'httpx'} & sys.modules.keys()
""".replace(
        "PATHS",
        repr([str(ROOT / "src"), str(ROOT)]),
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
