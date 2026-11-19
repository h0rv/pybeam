import logging
from typing import override

from examples.agents import app as agents_app
from examples.agents import runtime

app = agents_app.app
overflow_counts: dict[str, int] = {"snapshot": 0, "sse": 0}
runtime.SNAPSHOT_PENDING_BYTES = 1024
agents_app.TRANSCRIPT_PENDING_BYTES = 1024


class OverflowCapture(logging.Handler):
    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.msg == "Snapshot subscription overflow; resubscribing":
            overflow_counts["snapshot"] += 1
            runtime.SNAPSHOT_PENDING_BYTES = 1024 * 1024
        elif record.msg == "Transcript subscription overflow; resubscribing session %s":
            overflow_counts["sse"] += 1
            agents_app.TRANSCRIPT_PENDING_BYTES = 65536


capture = OverflowCapture()
logging.getLogger("examples.agents.runtime").addHandler(capture)
logging.getLogger("examples.agents.app").addHandler(capture)


@app.get("/test/overflows")
async def overflows() -> dict[str, int]:
    return overflow_counts.copy()
