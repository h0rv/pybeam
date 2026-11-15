import json
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import TypeAdapter

from examples.distributed.config import configuration
from examples.distributed.models import (
    CleanupObserved,
    DemoProof,
    NodeObserved,
    Observation,
    StateObserved,
    exchange,
    sanitized,
)
from pybeam.distribution import HandlerIdentity, LocalCluster, Reply
from pybeam.runtime.guardian import GuardianEndpoint
from pybeam.runtime.ownership import Identity, durable_write


def continue_cli() -> None:
    return None


def wait_file(path: Path, *, seconds: float = 12, check: Callable[[], None] = continue_cli) -> str:
    deadline = time.monotonic() + seconds
    while not path.exists():
        check()
        if time.monotonic() > deadline:
            raise TimeoutError("Missing cluster event: " + path.name)
        time.sleep(0.02)
    check()
    return path.read_text()


def demonstration(
    *,
    guardian: GuardianEndpoint | None = None,
    observe: Callable[[Observation], None] | None = None,
    output: Path | None = None,
    check: Callable[[], None] = continue_cli,
) -> dict[str, object]:
    config, distribution = configuration()
    cluster = LocalCluster(distribution, config=config, guardian=guardian)
    observations: list[Observation] = []

    def active() -> None:
        check()
        if guardian is not None:
            guardian.require_active()

    def emit(event: Observation) -> None:
        if len(observations) >= 16:
            raise RuntimeError("Walkthrough observation budget exceeded")
        observations.append(event)
        if observe is not None:
            observe(event)

    def read(name: str) -> str:
        return wait_file(cluster.directory / name, check=active)

    active()
    with cluster:
        first = Reply.model_validate_json(read("first.json"))
        a = Identity.model_validate_json(read("a.birth.json"))
        b = Identity.model_validate_json(read("b.birth.json"))
        old = HandlerIdentity.model_validate_json(read("b.identity.json"))
        if a == b or a.pid == b.pid or first.identity is None:
            raise RuntimeError("Expected distinct native VM OS identities and sender handler")
        emit(NodeObserved(alias="a", birth=a, handler=first.identity, status="observed"))
        emit(NodeObserved(alias="b", birth=b, handler=old, status="observed"))
        emit(exchange(first))
        (cluster.directory / "start-inflight").touch()
        inflight = Reply.model_validate_json(read("inflight.json"))
        if inflight.worker is None or inflight.status != "pending":
            raise RuntimeError("No actual recipient task was running before stop")
        emit(StateObserved(state="task_pending", handler=inflight.identity, worker=inflight.worker))
        active()
        cluster.stop("b")
        deadline = time.monotonic() + 5
        while True:
            down = Reply.model_validate_json(read("a.state.json"))
            if (
                old.node not in down.nodes
                and down.events
                and down.events[-1].event == "nodedown"
                and down.events[-1].node == old.node
            ):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("A did not observe monitor_node DOWN")
            time.sleep(0.02)
        (cluster.directory / "observe-absent").touch()
        absent = Reply.model_validate_json(read("absent.json"))
        loss = Reply.model_validate_json(read("inflight-loss.json"))
        if loss.status != "uncertain":
            raise RuntimeError("Lost in-flight outcome was reported as certain")
        if absent.status != "uncertain" or absent.receipt is not None:
            raise RuntimeError("Absent B produced a success receipt")
        emit(StateObserved(state="nodedown_uncertain", handler=down.identity))
        (cluster.directory / "b.identity.json").unlink()
        (cluster.directory / "b.birth.json").unlink()
        active()
        cluster.restart("b")
        new = HandlerIdentity.model_validate_json(read("b.identity.json"))
        restarted = Identity.model_validate_json(read("b.birth.json"))
        if new.incarnation == old.incarnation or restarted == b:
            raise RuntimeError("Restart reused recipient incarnation or OS birth identity")
        emit(NodeObserved(alias="b", birth=restarted, handler=new, status="fresh"))
        (cluster.directory / "rejoin").touch()
        fresh = Reply.model_validate_json(read("fresh.json"))
        if fresh.identity != new or fresh.count != 0 or fresh.completed != 0:
            raise RuntimeError("Explicit rejoin did not observe empty fresh state")
        emit(StateObserved(state="fresh_empty", handler=fresh.identity))
        stale = Reply.model_validate_json(read("stale.json"))
        if stale.status != "stale":
            raise RuntimeError("Delayed old-generation ACK was accepted")
        emit(StateObserved(state="stale_ack_rejected", handler=stale.identity))
        second = Reply.model_validate_json(read("second.json"))
        emit(exchange(second))
        controls = Reply.model_validate_json(read("controls.json"))
        epmd = cluster.identities[0]
    if cluster.cleanup is None:
        raise RuntimeError("No guardian cleanup journal")
    proof = DemoProof(
        epmd=epmd,
        vm_a=a,
        vm_b=b,
        restarted_b=restarted,
        first=first,
        down=down,
        absent=absent,
        fresh=fresh,
        inflight=inflight,
        inflight_loss=loss,
        controls=controls,
        stale_ack=stale,
        second=second,
        cleanup=cluster.cleanup,
        shutdowns=tuple(cluster.shutdowns),
    )
    projected = sanitized(proof)
    emit(
        CleanupObserved(
            state="confirmed" if cluster.cleanup.phase == "confirmed" else "uncertain",
            owned=cluster.cleanup.owned,
        )
    )
    if projected.observations != tuple(observations):
        raise RuntimeError("Final proof differs from actual observation history")
    if output is not None:
        durable_write(output, projected.model_dump_json())
    return TypeAdapter(dict[str, object]).validate_json(proof.model_dump_json())


def main() -> None:
    print(json.dumps(demonstration(), separators=(",", ":")))


if __name__ == "__main__":
    main()
