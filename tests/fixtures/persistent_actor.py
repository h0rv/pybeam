import asyncio
from dataclasses import field

from pybeam import actor, write_state


@actor
class Progress:
    step: int = 0
    history: list[int] = field(default_factory=list)
    private: bytes = field(init=False, default=b"initial")

    async def advance(self, step: int, *, save: bool = False) -> int:
        self.step = step
        self.history.append(step)
        self.private = b"changed"
        return await write_state(self) if save else 0

    async def read(self) -> tuple[int, list[int], bytes]:
        return self.step, self.history, self.private

    async def child_write(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(write_state(self))
