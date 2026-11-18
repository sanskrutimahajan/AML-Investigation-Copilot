# API and workbench image (docker compose up). Ollama stays on the host for GPU access (ADR 009);
# the containers reach it at host.docker.internal. Data is never baked in: the graph lives in the
# Neo4j volume and names in the Qdrant volume, loaded once with `make seed`.
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface

WORKDIR /app
# Dependencies first, so code changes do not reinstall them. `er` holds the embedding model the
# API uses for name linking; `ui` holds Streamlit. Dev tools are left out.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-default-groups --group er --group ui --no-install-project

COPY src ./src
COPY ui ./ui
COPY .streamlit ./.streamlit
RUN uv sync --locked --no-default-groups --group er --group ui

RUN useradd --create-home --uid 1000 app && mkdir -p /cache && chown app /cache
USER app
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000 8501
CMD ["uvicorn", "tracegraph.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
