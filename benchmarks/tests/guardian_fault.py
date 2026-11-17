import os
import sys
import time
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from pybeam.codec import Json

from benchmarks import guardian
from benchmarks.contract import Identity
from benchmarks.ownership import Registry, durable_write, identity


def main() -> None:
    mode = sys.argv.pop(1)
    original = guardian.record

    def pause_spawn(
        directory: Path,
        owned: set[Identity],
        phase: Literal["tracking", "spawning", "confirmed", "uncertain"],
    ) -> None:
        original(directory, owned, phase)
        if phase == "spawning" and len(owned) == (0 if mode == "adapter_start" else 1):
            durable_write(
                directory / "startup-guardian.json",
                Json(type(identity(os.getpid()))).encode(identity(os.getpid())).decode(),
            )
            durable_write(
                directory / "startup-owned.json",
                Json(type(Registry(phase=phase, owned=tuple(owned))))
                .encode(Registry(phase=phase, owned=tuple(owned)))
                .decode(),
            )
            time.sleep(30)

    with patch("benchmarks.guardian.record", new=pause_spawn):
        guardian.main()


if __name__ == "__main__":
    main()
