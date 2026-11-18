from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from examples.sqlalchemy.main import get_database, router
from examples.sqlalchemy.store import Database, Mode, databases


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_notes__dependency_override_without_registry(tmp_path: Path, mode: Mode) -> None:
    db = Database(tmp_path)
    app = FastAPI()
    app.include_router(router)

    async def get_test_database() -> Database:
        return db

    app.dependency_overrides[get_database] = get_test_database
    assert "notes" not in databases
    try:
        await db.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/notes/" + mode)).json() == []
            response = await client.post("/notes", json={"mode": mode, "text": "saved"})
            assert response.status_code == 200
            assert response.json() == "saved"
            duplicate = await client.post("/notes", json={"mode": mode, "text": "saved"})
            assert duplicate.status_code == 409
            failed = await client.post(
                "/notes", json={"mode": mode, "text": "rollback", "fail": True}
            )
            assert failed.status_code == 409
            assert (await client.get("/notes/" + mode)).json() == ["saved"]
    finally:
        app.dependency_overrides.clear()
        await db.close()
    assert "notes" not in databases
