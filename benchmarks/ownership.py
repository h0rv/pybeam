import os

from pybeam.runtime.guardian import (
    SpawnReply as SpawnReply,
)
from pybeam.runtime.guardian import (
    SpawnRequest as SpawnRequest,
)
from pybeam.runtime.guardian import (
    spawn_owned as spawn_owned,
)
from pybeam.runtime.ownership import (
    Registry as Registry,
)
from pybeam.runtime.ownership import (
    cleanup as cleanup,
)
from pybeam.runtime.ownership import (
    discover as discover,
)
from pybeam.runtime.ownership import (
    durable_write as durable_write,
)
from pybeam.runtime.ownership import (
    identity as identity,
)
from pybeam.runtime.ownership import (
    record as record,
)
from pybeam.runtime.ownership import (
    resolve as resolve,
)

from benchmarks.contract import Identity, StrictModel


class Lease(StrictModel):
    trial_directory: str


def write_lease(descriptor: int, lease: Lease | None) -> None:
    payload = b"" if lease is None else lease.model_dump_json().encode()
    os.lseek(descriptor, 0, os.SEEK_SET)
    written = os.write(descriptor, payload)
    if written != len(payload):
        raise OSError("Incomplete load lease write; ownership must be inspected")
    os.ftruncate(descriptor, len(payload))
    os.fsync(descriptor)


class Control(StrictModel):
    owner: Identity
    stop_file: str
