import ast
import re
import subprocess
from pathlib import Path

from tests.test_actors_typing import ROOT, checker


def test_pythonic_actor_types__preserve_constructor_methods_and_returns() -> None:
    result = subprocess.run(
        [checker(), "check", "--error-on-warning", "tests/pythonic_actors_positive.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pythonic_actor_types__reject_every_negative_case(tmp_path: Path) -> None:
    source = (ROOT / "tests/pythonic_actors_negative.txt").read_text()
    negative = tmp_path / "negative.py"
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
    assert len(functions) == 15
    for function in functions:
        assert function.end_lineno is not None
        assert any(function.lineno <= line <= function.end_lineno for line in lines), (
            function.name,
            output,
        )
