FROM ghcr.io/astral-sh/uv:0.13.8 AS uv
FROM python:3.14-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY main.py worker.py ./
COPY templates ./templates
COPY static ./static
ENV PATH="/app/.venv/bin:$PATH"
