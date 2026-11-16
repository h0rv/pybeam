import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    not (ROOT.parent / "erlang-python/priv/erlang_services.py").is_file(),
    reason="requires sibling erlang-python Python service implementation",
)


def test_services__missing_native_module_has_clear_error() -> None:
    code = """
import sys
sys.path.insert(0, SOURCE)
sys.modules['erlang_services'] = None
import pybeam
import pybeam.services

try:
    import pybeam.services.background
except RuntimeError as error:
    assert str(error) == 'Native background services are required; no Python fallback'
    assert isinstance(error.__cause__, ModuleNotFoundError)
    assert error.__cause__.name == 'erlang_services'
else:
    raise AssertionError('missing native services must be rejected')
""".replace("SOURCE", repr(str(ROOT / "src")))
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_services__imports_before_starting_the_vm() -> None:
    paths = [str(ROOT / "src"), str(ROOT.parent / "erlang-python" / "priv")]
    code = "\n".join(
        (
            "import sys",
            "sys.path[:0] = " + repr(paths),
            "from pybeam.services.background import ServiceRef, ServiceSlot, ServiceState",
            "slot = ServiceSlot(capacity=1)",
            "assert 'erlang' not in sys.modules",
            "assert '_erlang_impl' not in sys.modules",
            "assert not {'hornbeam', 'fastapi', 'litestar', 'pyview'} & sys.modules.keys()",
        )
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


def test_services__no_web_dependency_or_standard_loop_fallback() -> None:
    code = """
import asyncio
import sys
sys.path[:0] = PATHS
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
        repr([str(ROOT / "src"), str(ROOT.parent / "erlang-python" / "priv")]),
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
