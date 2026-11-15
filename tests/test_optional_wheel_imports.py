import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.runtime
@pytest.mark.parametrize(
    ("extra", "modules"),
    [
        ("fastapi", ("fastapi.dependencies",)),
        ("litestar", ("litestar.channels", "litestar.store")),
        ("pyview", ("pyview.subscriptions",)),
    ],
)
def test_installed_extra__imports_without_other_frameworks_or_vm(
    extra: str, modules: tuple[str, ...], tmp_path: Path
) -> None:
    wheel_value = os.environ.get("PYBEAM_TEST_WHEEL")
    if wheel_value is None:
        pytest.skip("Set PYBEAM_TEST_WHEEL for isolated installed-extra gates")
    wheel = Path(wheel_value).resolve(strict=True)
    uv = shutil.which("uv")
    assert uv is not None
    environment = tmp_path / "environment"
    interpreter = environment / "bin/python"
    subprocess.run([uv, "venv", "--python", sys.executable, str(environment)], check=True)
    subprocess.run(
        [uv, "pip", "install", "--python", str(interpreter), str(wheel) + "[" + extra + "]"],
        check=True,
        timeout=120,
    )
    forbidden = {"hornbeam", "erlang", "fastapi", "litestar", "pyview"} - {extra}
    imports = "\n".join("import pybeam.integrations." + module for module in modules)
    script = (
        "import sys\n"
        "import pybeam\n"
        "from pybeam.services.background import ServiceSlot\n"
        "from pybeam.integrations.hornbeam.client import Beam\n"
        + imports
        + "\nassert not "
        + repr(forbidden)
        + " & sys.modules.keys()\nprint(pybeam.__file__)\n"
    )
    result = subprocess.run(
        [str(interpreter), "-I", "-c", script],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(environment / "lib/python3.14/site-packages/pybeam/__init__.py") in result.stdout
