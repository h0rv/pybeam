from examples.agents.protocol import Snapshot
from pybeam import actor, write_state


@actor
class Session:
    snapshot_json: str = "{}"

    async def read(self) -> str:
        return self.snapshot_json

    async def save(self, payload: str) -> str:
        snapshot = Snapshot.model_validate_json(payload)
        snapshot.revision = Snapshot.model_validate_json(self.snapshot_json).revision + 1
        self.snapshot_json = snapshot.model_dump_json()
        await write_state(self)
        return self.snapshot_json
