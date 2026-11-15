from dataclasses import dataclass, field

import erlang
from _erlang_impl._supervision import _require_available


@dataclass(frozen=True, slots=True)
class Started:
    process: object


@dataclass(frozen=True, slots=True)
class Stopped:
    process: object
    reason: str


@dataclass(frozen=True, slots=True)
class DemoEvents:
    channel: erlang.Channel

    @classmethod
    def attach(cls, resource: object) -> DemoEvents:
        return cls(erlang.Channel(resource))

    async def next_event(self) -> Started | Stopped:
        event = await self.channel.async_receive(native_only=True)
        if isinstance(event, tuple):
            if len(event) == 2 and event[0] == "started":
                return Started(event[1])
            if len(event) == 3 and event[0] == "stopped" and isinstance(event[2], str):
                return Stopped(event[1], event[2])
        raise RuntimeError("invalid native regression event", event)


@dataclass(frozen=True, slots=True)
class DemoCounter:
    process: object
    capacity: int | None = field(kw_only=True)
    max_bytes: int | None = field(kw_only=True)

    def __post_init__(self) -> None:
        _require_available(self.capacity, self.max_bytes, None)

    async def value(self) -> int:
        result = await erlang.async_call("_supervision_demo_read", self.process)
        if type(result) is not int:
            raise RuntimeError("invalid native counter value", result)
        return result

    async def increment(self) -> int:
        result = await erlang.async_call("_supervision_demo_increment", self.process)
        if type(result) is not int:
            raise RuntimeError("invalid native counter value", result)
        return result

    async def crash(self) -> None:
        result = await erlang.async_call("_supervision_demo_crash", self.process)
        if result is not True:
            raise RuntimeError("native counter crash was not confirmed", result)
