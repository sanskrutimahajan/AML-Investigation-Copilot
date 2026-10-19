FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# Install locked dependencies first so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-default-groups --no-install-project
COPY src ./src
RUN uv sync --locked --no-default-groups

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "tracegraph.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
