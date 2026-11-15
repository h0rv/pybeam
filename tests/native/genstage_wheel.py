import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

import pybeam
from pybeam.actors import ActorGroup, context_counts


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

    from pybeam.runtime.optional import read_manifest

    root = Path(pybeam.__file__).parent / "runtime/_assets/optional/genstage"
    manifest = await asyncio.to_thread(read_manifest, root)
    compat = Path(__file__).with_name("compat") / "ebin"
    assert (
        await erlang.async_call(
            "_application_start",
            [str(compat).encode()],
            [(b"py_application_probe", b"0.1.0")],
            manifest.erts_version.encode(),
        )
        == "ok"
    )
    print(
        "COMPATIBILITY_PREFLIGHT_OK current-stale and old-only GenStage/logger rejected intact; matching and idempotent accepted"
    )
    from pybeam.integrations.genstage.pipeline import stream

    async with stream(16, process) as events:
        results = [Outcome.model_validate_json(event) async for event in events]
    assert [item.id for item in results] == list(range(16))
    assert all(item.detail == "HÉLLO" for item in results)
    proof = RuntimeProof.model_validate(await erlang.async_call("pybeam_genstage", b"runtime"))
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
    from discord_simulation import Room
    from genstage_coexist import blocked

    baseline = await context_counts()
    async with ActorGroup(max_children=2) as actors:
        counter = await actors.spawn(Counter, value=10)
        room = await actors.spawn(
            Room, room_id="coexist", max_members=2, max_events=2, history_limit=2
        )
        before = await actors.info(room)
        async with stream(1, blocked) as pending, asyncio.TaskGroup() as tasks:

            async def next_item() -> bytes:
                return await anext(pending)

            demand = tasks.create_task(next_item())
            assert await erlang.async_call("genstage_coexist_ready") == "ok"
            try:
                assert not demand.done()
                assert await counter.add(2) == 12
                assert await room.join("member", epoch=0) == 1
                await actors.crash(room)
                replacement = await actors.rejoin(room)
                after = await actors.info(replacement)
                assert before.pid != after.pid and before.generation != after.generation
                assert (await replacement.snapshot()).members == ()
                assert not demand.done()
            finally:
                assert await erlang.async_call("genstage_coexist_release") == "ok"
            assert Outcome.model_validate_json(await demand).id == 0
    assert (await context_counts()).live == baseline.live
    print("CURRENT_CORE_COEXISTENCE Counter/Room controls with blocked GenStage demand drained")
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
