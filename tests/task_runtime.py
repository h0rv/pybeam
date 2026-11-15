import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_native(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(ROOT / "_build/default") + '"\n'
    )
    script = tmp_path / "main.py"
    script.write_text(
        "import asyncio\n"
        "from pydantic import TypeAdapter\n"
        "import pybeam\n"
        "from pybeam.submissions import Overloaded\n"
        "async def main():\n"
        + body
        + '\n    print("native-submission-ok")\n'
        + "pybeam.run(main)\n"
    )
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT)))),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
