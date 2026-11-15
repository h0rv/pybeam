from pathlib import Path
from typing import Self, override

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
            ("first.json", proof.first.model_dump_json()),
            ("a.birth.json", proof.vm_a.model_dump_json()),
            ("b.birth.json", proof.vm_b.model_dump_json()),
            ("b.identity.json", handler.model_dump_json()),
            ("inflight.json", proof.inflight.model_dump_json()),
            ("controls.json", proof.controls.model_dump_json()),
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
            (self.directory / name).write_text(reply.model_dump_json())

    @override
    def restart(self, alias: str) -> Identity:
        if alias != "b" or self.restarts:
            raise ValueError("Controlled scenario permits only one B restart")
        self.restarts += 1
        handler: HandlerIdentity | None = self.proof.fresh.identity
        if handler is None:
            raise ValueError("Controlled fixture has no fresh recipient")
        for name, text in (
            ("b.identity.json", handler.model_dump_json()),
            ("b.birth.json", self.proof.restarted_b.model_dump_json()),
            ("fresh.json", self.proof.fresh.model_dump_json()),
            ("stale.json", self.proof.stale_ack.model_dump_json()),
            ("second.json", self.proof.second.model_dump_json()),
        ):
            (self.directory / name).write_text(text)
        return self.proof.restarted_b

    @override
    def close(self) -> None:
        self.closed = True
        self.cleanup = self.proof.cleanup
