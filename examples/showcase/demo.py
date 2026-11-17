import site
import sys
import time
from pathlib import Path

from benchmarks.contract import Unsupported
from benchmarks.ownership import spawn_owned
from examples.showcase.catalog import ROOT, catalog
from pybeam.codec import Json


def main() -> None:
    key, trusted_launcher, directory = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    trusted_root = None if trusted_launcher == "" else Path(trusted_launcher).parents[1]
    card = next(card for card in catalog(trusted_root) if card.key == key)
    if card.disabled is not None:
        raise RuntimeError(card.disabled)
    if key == "hold":
        argv = (
            sys.executable,
            "-c",
            "import time; print('Owned child waiting', flush=True); time.sleep(25)",
        )
    else:
        if card.launcher is None:
            raise RuntimeError("No explicitly trusted launcher")
        argv = (
            "escript",
            str(card.launcher),
            str(ROOT / "_build/hornbeam"),
            "examples." + key + ".main:app",
            "127.0.0.1:" + str(card.port),
            "on",
            str(ROOT),
            str(ROOT / "src"),
            site.getsitepackages()[0],
        )
    owned = spawn_owned(directory, argv)
    print("Guardian owns child PID " + str(owned.pid), flush=True)
    time.sleep(25)
    result = Unsupported(reason="Timed demo session ended; no benchmark metrics", features=(key,))
    (directory / "adapter.json").write_text(Json(type(result)).encode(result).decode())


if __name__ == "__main__":
    main()
