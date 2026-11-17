import asyncio
from dataclasses import dataclass, fields
from pathlib import Path
from typing import get_type_hints

from pybeam.codec import Json

try:
    import erlang
except ModuleNotFoundError as error:
    if error.name != "erlang":
        raise
    erlang = None


class ActorStateError(RuntimeError):
    """Storage failed; an interrupted write may already have committed."""


class ActorStateConflictError(ActorStateError):
    """Another writer advanced the stored version. Restart before writing again."""


_OBJECT = Json(dict[str, object])


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    key: bytes
    directory: Path
    fields: tuple[tuple[str, Json[object]], ...]
    max_bytes: int

    @classmethod
    def build(
        cls, actor: type[object], key: str, directory: Path, max_bytes: int
    ) -> SnapshotSpec:
        if not key:
            raise ValueError("Actor state key must be nonempty")
        identity = _OBJECT.encode({"actor": actor.__module__ + "." + actor.__qualname__, "key": key})
        if len(identity) > 4096:
            raise ValueError("Encoded actor state key exceeds 4096 bytes")
        hints = get_type_hints(actor)
        return cls(
            identity,
            directory,
            tuple((item.name, Json(hints[item.name])) for item in fields(actor)),
            min(max_bytes, 1_048_576),
        )

    def encode(self, state: object) -> bytes:
        payload = _OBJECT.encode(
            {name: codec.dump(object.__getattribute__(state, name)) for name, codec in self.fields}
        )
        if len(payload) > self.max_bytes:
            raise ActorStateError("Encoded actor state exceeds max_bytes")
        return payload

    def decode(self, payload: bytes) -> dict[str, object]:
        if len(payload) > self.max_bytes:
            raise ActorStateError("Stored actor state exceeds max_bytes")
        data = _OBJECT.decode(payload)
        if set(data) != {name for name, _ in self.fields}:
            raise ActorStateError("Stored actor fields do not match the declared state")
        return {name: codec.load(data[name]) for name, codec in self.fields}


async def _call(name: str, *args: object) -> object:
    if erlang is None:
        raise ActorStateError("Actor persistence requires the managed runtime")
    try:
        result = await erlang.async_call(name, *args)
    except Exception as error:
        raise ActorStateError("Actor state operation failed: " + name) from error
    if isinstance(result, tuple) and len(result) == 2 and result[0] == "error":
        reason = result[1]
        if isinstance(reason, tuple) and len(reason) == 3 and reason[0] == "version_conflict":
            raise ActorStateConflictError("Stored actor version changed: " + repr(reason[1:]))
        raise ActorStateError("Actor state operation failed: " + repr(reason))
    return result


class Snapshot:
    def __init__(self, spec: SnapshotSpec) -> None:
        self.spec = spec
        self.version = 0
        self.writable = True

    async def restore(self, state: object) -> None:
        if await _call("_actor_state_open", str(self.spec.directory)) != "ok":
            raise ActorStateError("Invalid actor state open response")
        result = await _call("_actor_state_read", self.spec.key)
        if result == "missing":
            return
        if isinstance(result, tuple) and len(result) == 3:
            status, version, payload = result
            if status == "ok" and type(version) is int and version > 0 and isinstance(payload, bytes):
                values = self.spec.decode(payload)
                for name, value in values.items():
                    object.__setattr__(state, name, value)
                self.version = version
                return
        raise ActorStateError("Invalid actor state read response")

    async def _commit(self, payload: bytes) -> int:
        try:
            result = await _call("_actor_state_write", self.spec.key, self.version, payload)
            if result != ("ok", self.version + 1):
                raise ActorStateError("Invalid actor state write response")
            self.version += 1
            return self.version
        except BaseException:
            self.writable = False
            raise

    async def write(self, state: object) -> int:
        if not self.writable:
            raise ActorStateError("Actor state write outcome is uncertain; restart before writing")
        payload = self.spec.encode(state)
        pending = asyncio.create_task(self._commit(payload))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
            pending.result()
            raise
