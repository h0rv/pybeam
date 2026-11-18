from fastapi import FastAPI

from examples.sqlalchemy.main import router
from tests.fixtures.sqlalchemy_app import pool_stats, sync_lifespan

app = FastAPI(lifespan=sync_lifespan)
app.include_router(router)
app.get("/pool-stats")(pool_stats)
