import subprocess
import sys

CHECK = """
import sys
from concurrent import interpreters
try:
    __import__({name!r})
except ImportError as error:
    print("not installed:", error)
    raise SystemExit
gil = sys._is_gil_enabled()
try:
    interpreters.create().exec("import " + {name!r})
except interpreters.ExecutionFailed as error:
    print("gil", gil, "subinterpreter failed:", error.excinfo.formatted.splitlines()[-1])
else:
    print("gil", gil, "subinterpreter ok")
"""


def main() -> None:
    for name in sys.argv[1:]:
        code = CHECK.format(name=name)
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-W", "ignore", "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        print(name, (done.stdout or done.stderr).strip().splitlines()[-1])


if __name__ == "__main__":
    main()
