from pathlib import Path
from typing import Self, override

from pybeam.codec import Json
from pybeam.distribution import Distribution, HandlerIdentity, LocalCluster
from pybeam.runtime.config import Config
from pybeam.runtime.guardian import GuardianEndpoint
from pybeam.runtime.ownership import Identity
from tests.showcase_fixtures import controlled_proof


class ControlledCluster(LocalCluster):
    def __init__(
        self,
        distribution: Distribution,
        *,
        directory: Path,
        config: Config | None = None,
        guardian: GuardianEndpoint | None = None,
    ) -> None:
        super().__init__(distribution, config=config, guardian=guardian)
        self.controlled_directory = directory
        self.proof = controlled_proof()
        self.closed = False
        self.starts = 0
        self.restarts = 0

    @property
    @override
    def directory(self) -> Path:
        return self.controlled_directory

    @override
    def __enter__(self) -> Self:
        self.starts += 1
        proof = self.proof
        self.identities.append(proof.epmd)
        handler = proof.first.recipient
        if handler is None:
            raise ValueError("Controlled fixture has no recipient")
        for name, text in (
            ("first.json", Json(type(proof.first)).encode(proof.first).decode()),
            ("a.birth.json", Json(type(proof.vm_a)).encode(proof.vm_a).decode()),
            ("b.birth.json", Json(type(proof.vm_b)).encode(proof.vm_b).decode()),
            ("b.identity.json", Json(type(handler)).encode(handler).decode()),
            ("inflight.json", Json(type(proof.inflight)).encode(proof.inflight).decode()),
            ("controls.json", Json(type(proof.controls)).encode(proof.controls).decode()),
        ):
            (self.directory / name).write_text(text)
        return self

    @override
    def stop(self, alias: str) -> None:
        if alias != "b":
            raise ValueError("Controlled scenario stops only B")
        for name, reply in (
            ("a.state.json", self.proof.down),
            ("absent.json", self.proof.absent),
            ("inflight-loss.json", self.proof.inflight_loss),
        ):
            (self.directory / name).write_text(Json(type(reply)).encode(reply).decode())

    @override
    def restart(self, alias: str) -> Identity:
        if alias != "b" or self.restarts:
            raise ValueError("Controlled scenario permits only one B restart")
        self.restarts += 1
        handler: HandlerIdentity | None = self.proof.fresh.identity
        if handler is None:
            raise ValueError("Controlled fixture has no fresh recipient")
        for name, text in (
            ("b.identity.json", Json(type(handler)).encode(handler).decode()),
            (
                "b.birth.json",
                Json(type(self.proof.restarted_b)).encode(self.proof.restarted_b).decode(),
            ),
            ("fresh.json", Json(type(self.proof.fresh)).encode(self.proof.fresh).decode()),
            ("stale.json", Json(type(self.proof.stale_ack)).encode(self.proof.stale_ack).decode()),
            ("second.json", Json(type(self.proof.second)).encode(self.proof.second).decode()),
        ):
            (self.directory / name).write_text(text)
        return self.proof.restarted_b

    @override
    def close(self) -> None:
        self.closed = True
        self.cleanup = self.proof.cleanup
