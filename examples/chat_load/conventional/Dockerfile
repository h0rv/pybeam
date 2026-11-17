FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv
WORKDIR /srv
COPY examples/chat_load/conventional/pyproject.toml examples/chat_load/conventional/uv.lock examples/chat_load/conventional/
RUN uv sync --locked --no-dev --project examples/chat_load/conventional
ENV PATH=/srv/examples/chat_load/conventional/.venv/bin:$PATH PYTHONPATH=/srv
COPY examples/__init__.py examples/
COPY examples/chat_load/__init__.py examples/chat_load/protocol.py examples/chat_load/rooms.py examples/chat_load/jobs.py examples/chat_load/faults.py examples/chat_load/origin.py examples/chat_load/
COPY examples/chat_load/conventional/*.py examples/chat_load/conventional/
