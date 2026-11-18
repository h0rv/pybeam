# SQLAlchemy notes

Run a SQLite notes API with startup-owned SQLAlchemy sync and async engines,
PyBEAM actors, and task fan-out. Each operation creates its own session; the
application reuses two bounded connection pools until shutdown.

```console
uv run --extra fastapi --extra hornbeam python -m examples.sqlalchemy.main
```

SQLAlchemy and aiosqlite are development dependencies in this checkout, not
dependencies of `python-beam`. The API listens on `127.0.0.1:8765` and stores
`sync.sqlite3` and `async.sqlite3` in the current directory. Use `--port` to
choose another port. Stop the application with Ctrl+C to dispose both engines.

```console
curl -X POST http://127.0.0.1:8765/notes -H 'Content-Type: application/json' -d '{"mode":"sync","text":"Buy milk"}'
curl -X POST http://127.0.0.1:8765/actors -H 'Content-Type: application/json' -d '{"mode":"async","text":"Write tests"}'
curl -X POST 'http://127.0.0.1:8765/tasks/async?prefix=todo-'
curl http://127.0.0.1:8765/notes/async
```

`/notes` writes directly, `/actors` writes through a short-lived `@actor`, and
`/tasks/{mode}` starts four dedicated-loop tasks. Both `sync` and `async` modes
work through all three paths. Notes have unique text; choose a new prefix when
repeating the fan-out. Set `"fail":true` on a `/notes` request to roll back its
transaction and return HTTP 409. Actor or task failure does not replay a write.
SQLite persists committed notes across actor restarts; supervision does not
make uncommitted work durable or undo an already committed write.

## Pool ownership

`store.py` shares engines, never sessions or checked-out connections. The sync
engine sets `check_same_thread=False` because calls can use different native
threads. Blocking sync operations run with `asyncio.to_thread`; transactions
return their connections on success and failure. SQLite still serializes writers.

A pooled async engine belongs to the startup loop. Actor turns can move between
worker loops, and dedicated-loop tasks have separate loops. `Database.on_owner`
explicitly submits async database operations to the startup loop instead of
moving pooled connections between loops. The runtime regression demonstrates
that direct foreign-loop use fails under pool contention. The module-level
registry belongs to this single application in one interpreter, not to a
distributed service or an isolated-process task. Shutdown must drain callers
before disposing engines. Cancellation or a lost reply does not prove rollback.

## Serving

The entrypoint uses `pybeam.integrations.hornbeam.run` with managed Uvicorn.
This keeps lifespan and HTTP handlers on the managed driver loop. It is not
Hornbeam's native ASGI HTTP server.
