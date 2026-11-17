import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.native.owngil_entries import waiting


async def test_waiting__cancellation_at_marker_publication_runs_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "running"
    published = asyncio.Event()
    blocked = asyncio.Event()
    to_thread = asyncio.to_thread

    async def pause_after_publication[**P, R](
        function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        result = await to_thread(function, *args, **kwargs)
        if args == ("running",):
            published.set()
            await blocked.wait()
        return result

    monkeypatch.setattr(asyncio, "to_thread", pause_after_publication)
    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        task = group.create_task(waiting(json.dumps(str(marker)).encode()))
        await published.wait()
        task.cancel()
    assert task.cancelled()
    assert marker.read_text() == "finished"
