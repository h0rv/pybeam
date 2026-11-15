import ast
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def checker() -> str:
    local = ROOT / ".venv" / "bin" / "ty"
    path = str(local) if local.is_file() else shutil.which("ty")
    assert path is not None, "Install the existing dev dependency group"
    return path


def test_actor_types__preserve_arguments_and_awaited_return() -> None:
    result = subprocess.run(
        [checker(), "check", "--error-on-warning", "tests/actors_positive.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_actor_types__reject_every_negative_case(tmp_path: Path) -> None:
    source = (ROOT / "tests/actors_negative.txt").read_text()
    negative = tmp_path / "actors_negative.py"
    negative.write_text(source)
    result = subprocess.run(
        [
            checker(),
            "check",
            "--project",
            str(ROOT),
            "--extra-search-path",
            str(ROOT),
            "--error-on-warning",
            "--output-format",
            "concise",
            str(negative),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    lines = [int(match[1]) for match in re.finditer(r":(\d+):\d+: error\[", output)]
    functions = [node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef)]
    assert len(functions) == 8
    for function in functions:
        end = function.end_lineno
        assert end is not None
        assert any(function.lineno <= line <= end for line in lines), (function.name, output)
