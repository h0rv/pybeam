import json
import time
from pathlib import Path

from examples.distributed.config import configuration
from examples.distributed.jobs import JobResult
from pybeam.distribution import HandlerIdentity, LocalCluster, Reply
from pybeam.runtime.ownership import Identity


def wait_file(path: Path, *, seconds: float = 12) -> str:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Missing cluster event: " + path.name)
        time.sleep(0.02)
    return path.read_text()


def demonstration() -> dict[str, object]:
    config, distribution = configuration()
    cluster = LocalCluster(distribution, config=config)
    with cluster:
        first = Reply.model_validate_json(wait_file(cluster.directory / "first.json"))
        a = Identity.model_validate_json(wait_file(cluster.directory / "a.birth.json"))
        b = Identity.model_validate_json(wait_file(cluster.directory / "b.birth.json"))
        old = HandlerIdentity.model_validate_json(wait_file(cluster.directory / "b.identity.json"))
        if a == b or a.pid == b.pid:
            raise RuntimeError("Expected distinct native VM OS identities")
        (cluster.directory / "start-inflight").touch()
        inflight = Reply.model_validate_json(wait_file(cluster.directory / "inflight.json"))
        if inflight.worker is None or inflight.status != "pending":
            raise RuntimeError("No actual recipient task was running before stop")
        cluster.stop("b")
        deadline = time.monotonic() + 5
        while True:
            down = Reply.model_validate_json(wait_file(cluster.directory / "a.state.json"))
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
        absent = Reply.model_validate_json(wait_file(cluster.directory / "absent.json"))
        loss = Reply.model_validate_json(wait_file(cluster.directory / "inflight-loss.json"))
        if loss.status != "uncertain":
            raise RuntimeError("Lost in-flight outcome was reported as certain")
        if absent.status != "uncertain" or absent.receipt is not None:
            raise RuntimeError("Absent B produced a success receipt")
        (cluster.directory / "b.identity.json").unlink()
        (cluster.directory / "b.birth.json").unlink()
        cluster.restart("b")
        new = HandlerIdentity.model_validate_json(wait_file(cluster.directory / "b.identity.json"))
        restarted = Identity.model_validate_json(wait_file(cluster.directory / "b.birth.json"))
        if new.incarnation == old.incarnation or restarted == b:
            raise RuntimeError("Restart reused recipient incarnation or OS birth identity")
        (cluster.directory / "rejoin").touch()
        fresh = Reply.model_validate_json(wait_file(cluster.directory / "fresh.json"))
        second = Reply.model_validate_json(wait_file(cluster.directory / "second.json"))
        for proof in (first, second):
            if proof.result is None or JobResult.model_validate_json(proof.result) != JobResult(
                value=42, side_effect_count=1, python_version="3.14.10"
            ):
                raise RuntimeError("Result did not come from exactly one recipient task effect")
        proof: dict[str, object] = {
            "epmd": cluster.identities[0].model_dump(),
            "vm_a": a.model_dump(),
            "vm_b": b.model_dump(),
            "restarted_b": restarted.model_dump(),
            "first": first.model_dump(),
            "down": down.model_dump(),
            "absent": absent.model_dump(),
            "fresh": fresh.model_dump(),
            "inflight": inflight.model_dump(),
            "inflight_loss": loss.model_dump(),
            "controls": Reply.model_validate_json(
                wait_file(cluster.directory / "controls.json")
            ).model_dump(),
            "stale_ack": Reply.model_validate_json(
                wait_file(cluster.directory / "stale.json")
            ).model_dump(),
            "second": second.model_dump(),
        }
    if cluster.cleanup is None:
        raise RuntimeError("No guardian cleanup journal")
    proof["cleanup"] = cluster.cleanup.model_dump()
    proof["shutdowns"] = [outcome.model_dump() for outcome in cluster.shutdowns]
    return proof


def main() -> None:
    print(json.dumps(demonstration(), separators=(",", ":")))


if __name__ == "__main__":
    main()
