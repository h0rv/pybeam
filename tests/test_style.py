import ast
from pathlib import Path
from subprocess import run
from sys import executable

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/pybeam"


def python_files() -> list[Path]:
    return sorted(
        path
        for directory in ("src", "examples", "tests", "scripts", "typings")
        for path in (ROOT / directory).rglob("*")
        if path.suffix in {".py", ".pyi"} and "__pycache__" not in path.parts
    )


def test_owned_python__has_no_fstrings_or_relative_imports() -> None:
    violations: list[str] = []
    for path in python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        if ast.get_docstring(tree) is not None:
            violations.append("{}: module docstring".format(path.relative_to(ROOT)))
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                violations.append("{}:{}: f-string".format(path.relative_to(ROOT), node.lineno))
            if isinstance(node, ast.ImportFrom) and node.level:
                violations.append(
                    "{}:{}: relative import".format(path.relative_to(ROOT), node.lineno)
                )
    assert not violations, "\n".join(violations)


def test_core__does_not_import_optional_frameworks() -> None:
    forbidden = {
        "hornbeam",
        "erlang",
        "fastapi",
        "litestar",
        "pyview",
        "starlette",
        "htpy",
        "jinja2",
    }
    paths = [
        PACKAGE / "__init__.py",
        PACKAGE / "mailbox.py",
        PACKAGE / "errors.py",
        PACKAGE / "services" / "__init__.py",
        PACKAGE / "services" / "contracts.py",
    ]
    paths.extend((PACKAGE / "_shared").glob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, (path, name)
                assert not any(
                    name.startswith("pybeam." + plugin)
                    for plugin in ("fastapi", "litestar", "pyview")
                ), (path, name)


def test_subpackage_initializers__do_not_import_implementations() -> None:
    for path in PACKAGE.rglob("__init__.py"):
        if path in {
            PACKAGE / "__init__.py",
            PACKAGE / "actors" / "__init__.py",
            PACKAGE / "services" / "__init__.py",
        }:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree)), (
            path
        )


def test_core__imports_without_optional_dependencies() -> None:
    script = "\n".join(
        (
            "import sys",
            "sys.path.insert(0, 'src')",
            "from pybeam.services import ByteStore, Services, Subscription",
            "from pybeam.errors import SubscriptionClosed, SubscriptionOverflow",
            "from pybeam._shared.lifecycle import await_cleanup",
            "assert 'hornbeam' not in sys.modules",
            "assert 'erlang' not in sys.modules",
        )
    )
    result = run([executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
