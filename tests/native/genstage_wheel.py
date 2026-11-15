import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

import pybeam


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, value: int) -> int:
        self.value += value
        return self.value


class RuntimeProof(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    genstage: Literal["1.3.2"]
    elixir: Literal["1.20.0"]
    otp: Literal["29.1.2"]
    erts: Literal["17.1.1"]
    module: str
    module_md5: str


class OwnershipProof(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    before: Literal[1]
    after: Literal[1]
    idle: Literal[16]
    busy: Literal[2]
    ordered: Literal[16]
    batches: Literal[16]
    max_demand: Literal[1]
    initial_pulls: Literal[0]


async def main() -> None:
    import erlang

    assert sys.version_info[:3] == (3, 14, 10)
    assert "pybeam.integrations.genstage" not in sys.modules
    async with pybeam.spawn(Counter, value=10) as counter:
        assert await counter.add(2) == 12
    assert "pybeam.integrations.genstage" not in sys.modules
    from genstage_callbacks import Outcome, process

    from pybeam.integrations.genstage.pipeline import stream
    from pybeam.runtime.optional import read_manifest

    async with stream(16, process) as events:
        results = [Outcome.model_validate_json(event) async for event in events]
    assert [item.id for item in results] == list(range(16))
    assert all(item.detail == "HÉLLO" for item in results)
    proof = RuntimeProof.model_validate(await erlang.async_call("pybeam_genstage", b"runtime"))
    root = Path(pybeam.__file__).parent / "runtime/_assets/optional/genstage"
    manifest = await asyncio.to_thread(read_manifest, root)
    module = Path(proof.module)
    assert module.is_relative_to(root)
    declared = next(item for item in manifest.files if item.path == module.relative_to(root))
    content = await asyncio.to_thread(module.read_bytes)
    assert hashlib.sha256(content).hexdigest() == declared.sha256
    probe = Path(__file__).with_name("probe") / "ebin"
    assert (
        await erlang.async_call(
            "_application_start",
            [str(probe).encode()],
            [(b"pybeam_genstage_probe", b"0.1.0")],
            b"17.1.1",
        )
        == "ok"
    )
    owned = OwnershipProof.model_validate(await erlang.async_call("genstage_owned_probe"))
    print(
        "INSTALLED_PROOF",
        json.dumps(
            {
                "runtime": proof.model_dump(),
                "sha256": declared.sha256,
                "ownership": owned.model_dump(),
                "package": pybeam.__file__,
            }
        ),
        flush=True,
    )


pybeam.run(main)
