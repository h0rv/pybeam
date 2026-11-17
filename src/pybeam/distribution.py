import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from pybeam.codec import Json
from pybeam.runtime.cluster import LocalCluster as LocalCluster
from pybeam.runtime.config import Distribution as Distribution
from pybeam.runtime.config import PythonNode as PythonNode


@dataclass(frozen=True, slots=True, kw_only=True)
class HandlerIdentity:
    node: str
    pid: str
    incarnation: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeEvent:
    event: Literal["nodeup", "nodedown"]
    node: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Reply:
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


_HEX32 = re.compile(r"[0-9A-F]{32}")
_REPLY = Json(Reply)


class RequestTooLarge(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    op: str
    alias: Literal["a", "b"] | None = None
    ack_alias: Literal["a", "b"] | None = None
    id: str | None = None
    incarnation: str | None = None
    token: str | None = None
    definition: Literal["double"] | None = None
    size: int | None = None
    digest: str | None = None
    payload: str | None = None
    result: str | None = None
    worker: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.op) <= 32:
            raise ValueError("op must have 1 to 32 characters")
        if self.id is not None and not 1 <= len(self.id) <= 64:
            raise ValueError("id must have 1 to 64 characters")
        for name, value in (("incarnation", self.incarnation), ("token", self.token)):
            if value is not None and _HEX32.fullmatch(value) is None:
                raise ValueError(name + " must be 32 uppercase hexadecimal digits")
        if self.size is not None and not 0 < self.size <= 4096:
            raise ValueError("size must be from 1 to 4096")
        for name, value, limit in (
            ("payload", self.payload, 4096),
            ("result", self.result, 4096),
            ("worker", self.worker, 64),
        ):
            if value is not None and len(value) > limit:
                raise ValueError(name + " must have at most " + str(limit) + " characters")

    def encode(self) -> bytes:
        fields = _REQUEST.dump(self)
        if not isinstance(fields, dict):
            raise TypeError("Request did not encode as an object")
        present = {key: value for key, value in fields.items() if value is not None}
        data = json.dumps(present, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) > 8192:
            raise RequestTooLarge("Encoded demonstration request exceeds 8192 bytes")
        return data


_REQUEST = Json(Request)


async def local(request: Request) -> Reply:
    """Call the fixed local demonstration handler, not arbitrary Erlang MFA."""
    data = request.encode()
    from erlang import async_call

    value: object = await async_call("pybeam_dist_local", data)
    if not isinstance(value, str | bytes):
        raise RuntimeError("Distribution binding returned non-data")
    return _REPLY.decode(value)


async def remote(alias: Literal["a", "b"], request: Request) -> Reply:
    """Use OTP erpc to a configured alias. Uncertainty never permits automatic replay."""
    data = request.encode()
    from erlang import async_call

    value: object = await async_call("pybeam_dist_remote", alias.encode(), data)
    if not isinstance(value, str | bytes):
        raise RuntimeError("Distribution binding returned non-data")
    return _REPLY.decode(value)


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
