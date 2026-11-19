FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv
WORKDIR /srv
COPY examples/agents/usual/pyproject.toml examples/agents/usual/uv.lock examples/agents/usual/
RUN uv sync --locked --no-dev --project examples/agents/usual
ENV PATH=/srv/examples/agents/usual/.venv/bin:$PATH PYTHONPATH=/srv
COPY examples/__init__.py examples/
COPY examples/agents/__init__.py examples/agents/agent.py examples/agents/tools.py examples/agents/protocol.py examples/agents/views.py examples/agents/
COPY examples/agents/templates/ examples/agents/templates/
COPY examples/agents/static/ examples/agents/static/
COPY examples/static/ examples/static/
COPY examples/chat_load/__init__.py examples/chat_load/origin.py examples/chat_load/
COPY examples/agents/usual/*.py examples/agents/usual/
RUN mkdir -p /data/audit
