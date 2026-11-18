from tests.fixtures.sqlalchemy_app import app, sync_lifespan

app.router.lifespan_context = sync_lifespan
