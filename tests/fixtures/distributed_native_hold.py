import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import override

import pytest

from examples.distributed import demo
from examples.distributed.demo import continue_cli, demonstration
from examples.distributed.models import ExchangeObserved, NodeObserved, Observation
from pybeam.codec import Json
from pybeam.runtime.cluster import LocalCluster, NodeExit
from pybeam.runtime.guardian import GuardianEndpoint
from pybeam.runtime.ownership import Registry, durable_write, resolve


@dataclass(frozen=True, slots=True, kw_only=True)
class InnerCleanup:
    cleanup: Registry | None
    shutdowns: tuple[NodeExit, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeReady:
    job_id: str
    nodes: tuple[NodeObserved, NodeObserved]
    exchange: ExchangeObserved
    outer: Registry


def held_demonstration(
    *,
    guardian: GuardianEndpoint | None = None,
    observe: Callable[[Observation], None] | None = None,
    output: Path | None = None,
    check: Callable[[], None] = continue_cli,
) -> dict[str, object]:
    if guardian is None:
        raise ValueError("The controlled native hold requires its real borrowed guardian")

    class CapturedCluster(LocalCluster):
        @override
        def close(self) -> None:
            try:
                super().close()
            finally:
                durable_write(
                    guardian.directory / "inner-cleanup.json",
                    Json(type(InnerCleanup(cleanup=self.cleanup, shutdowns=tuple(self.shutdowns))))
                    .encode(InnerCleanup(cleanup=self.cleanup, shutdowns=tuple(self.shutdowns)))
                    .decode(),
                )

    nodes: list[NodeObserved] = []
    held = False

    def observed(event: Observation) -> None:
        nonlocal held
        if observe is not None:
            observe(event)
        if isinstance(event, NodeObserved):
            nodes.append(event)
        if isinstance(event, ExchangeObserved) and not held:
            if len(nodes) != 2 or {node.alias for node in nodes} != {"a", "b"}:
                raise ValueError("No actual two-node receiver-ready observation")
            outer = Json(Registry).decode((guardian.directory / "ownership.json").read_bytes())
            if any(resolve(node.birth) is None or node.birth not in outer.owned for node in nodes):
                raise ValueError("Native hold requires two live outer-acknowledged VM births")
            held = True
            durable_write(
                guardian.directory / "native-ready.json",
                Json(
                    type(
                        NativeReady(
                            job_id=guardian.directory.parents[1].name,
                            nodes=(nodes[0], nodes[1]),
                            exchange=event,
                            outer=outer,
                        )
                    )
                )
                .encode(
                    NativeReady(
                        job_id=guardian.directory.parents[1].name,
                        nodes=(nodes[0], nodes[1]),
                        exchange=event,
                        outer=outer,
                    )
                )
                .decode(),
            )
            deadline = time.monotonic() + 15
            while not (guardian.directory / "native-release").exists():
                check()
                guardian.require_active()
                if time.monotonic() >= deadline:
                    raise TimeoutError("Controlled native ready hold exceeded its test deadline")
                time.sleep(0.01)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(demo, "LocalCluster", CapturedCluster)
        return demonstration(guardian=guardian, observe=observed, output=output, check=check)
