import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from pybeam.runtime.cluster import LocalCluster as LocalCluster
from pybeam.runtime.config import Distribution as Distribution
from pybeam.runtime.config import PythonNode as PythonNode


class HandlerIdentity(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    node: str
    pid: str
    incarnation: str
    message_id: str | None = None


class NodeEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    event: Literal["nodeup", "nodedown"]
    node: str


class Reply(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    status: Literal[
        "ok",
        "uncertain",
        "unknown_node",
        "oversized",
        "stale",
        "duplicate",
        "overloaded",
        "unknown_token",
        "empty",
        "unknown_operation",
        "conflict",
        "pending",
        "invalid_phase",
        "admission_closed",
    ]
    identity: HandlerIdentity | None = None
    recipient: HandlerIdentity | None = None
    receipt: HandlerIdentity | None = None
    reverse_ack: Reply | None = None
    nodes: tuple[str, ...] = ()
    events: tuple[NodeEvent, ...] = ()
    events_dropped: int = 0
    count: int = 0
    definitions: tuple[Literal["double"], ...] = ()
    capacity: int = 0
    max_bytes: int = 0
    completed: int = 0
    otp_release: str | None = None
    otp_version: str | None = None
    erts_version: str | None = None
    private_home: bool = False
    token: str | None = None
    payload: str | None = None
    result: str | None = None
    worker: str | None = None
    worker_kind: Literal["temporary_otp_task"] | None = None
    worker_dead: bool = False
    job_id: str | None = None
    request_bytes: int | None = None
    cancel: bool = False


class RequestTooLarge(ValueError):
    pass


class Request(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    op: Annotated[str, Field(min_length=1, max_length=32)]
    alias: Literal["a", "b"] | None = None
    ack_alias: Literal["a", "b"] | None = None
    id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    incarnation: Annotated[str, Field(pattern=r"^[0-9A-F]{32}$")] | None = None
    token: Annotated[str, Field(pattern=r"^[0-9A-F]{32}$")] | None = None
    definition: Literal["double"] | None = None
    size: Annotated[int, Field(gt=0, le=4096)] | None = None
    digest: str | None = None
    payload: Annotated[str, Field(max_length=4096)] | None = None
    result: Annotated[str, Field(max_length=4096)] | None = None
    worker: Annotated[str, Field(max_length=64)] | None = None

    def encode(self) -> bytes:
        data = self.model_dump_json(exclude_none=True).encode()
        if len(data) > 8192:
            raise RequestTooLarge("Encoded demonstration request exceeds 8192 bytes")
        return data


async def local(request: Request) -> Reply:
    """Call the fixed local demonstration handler, not arbitrary Erlang MFA."""
    data = request.encode()
    from erlang import async_call

    value: object = await async_call("pybeam_dist_local", data)
    if not isinstance(value, str | bytes):
        raise RuntimeError("Distribution binding returned non-data")
    return Reply.model_validate_json(value)


async def remote(alias: Literal["a", "b"], request: Request) -> Reply:
    """Use OTP erpc to a configured alias. Uncertainty never permits automatic replay."""
    data = request.encode()
    from erlang import async_call

    value: object = await async_call("pybeam_dist_remote", alias.encode(), data)
    if not isinstance(value, str | bytes):
        raise RuntimeError("Distribution binding returned non-data")
    return Reply.model_validate_json(value)


def reservation(incarnation: str, message_id: str, payload: str) -> Request:
    encoded = payload.encode()
    return Request(
        op="reserve",
        incarnation=incarnation,
        id=message_id,
        definition="double",
        size=len(encoded),
        digest=hashlib.sha256(encoded).hexdigest(),
    )
