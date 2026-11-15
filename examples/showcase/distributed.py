import sys
from pathlib import Path

from examples.distributed.demo import demonstration
from examples.distributed.models import Observation
from examples.showcase.catalog import ROOT, distributed_unavailable
from pybeam.runtime.guardian import GuardianEndpoint


def walkthrough(directory: Path, guardian: GuardianEndpoint) -> None:
    reason = distributed_unavailable()
    if reason is not None:
        raise ValueError(reason)
    if Path.cwd().resolve() != ROOT:
        raise ValueError("Walkthrough must run from this source checkout")
    guardian.require_active()
    with (directory / "observations.jsonl").open("x") as stream:

        def observe(event: Observation) -> None:
            stream.write(event.model_dump_json() + "\n")
            stream.flush()

        demonstration(
            guardian=guardian,
            observe=observe,
            output=directory / "adapter.json",
            check=guardian.require_active,
        )


def main() -> None:
    directory = Path(sys.argv[1]).resolve()
    guardian = GuardianEndpoint.model_validate_json((directory / "guardian.json").read_text())
    if guardian.directory != directory:
        raise ValueError("Guardian registration directory differs from owned trial")
    walkthrough(directory, guardian)


if __name__ == "__main__":
    main()
