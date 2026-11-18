# ADR 014: `docker compose up` runs the whole app; `make seed` loads it

- **Status:** Accepted (2026-11-18). Phase 3 of the production-shape plan ([AUDIT.md](../../AUDIT.md)).
  Supersedes the "API runs natively" part of [ADR 001](001-local-service-orchestration.md) and
  [ADR 009](009-retrieval-and-qa.md) for normal use; running natively stays the development path.
- **Decided by the user (2026-11-18):** API and UI containers that call Ollama on the host.

## Decisions

1. **One image for the API and the workbench** (`Dockerfile`): Python 3.11 slim, dependencies from
   `uv.lock` (`er` group for the embedding model, `ui` group for Streamlit; no dev tools), non-root
   user, Hugging Face cache in a named volume. No data is baked in.
2. **Compose services:** `neo4j`, `qdrant`, `api` (:8000) and `ui` (:8501), all bound to
   127.0.0.1, with health checks and start order. The API reaches Neo4j and Qdrant by service name.
3. **Ollama stays on the host** (Docker on macOS has no GPU). Containers call
   `host.docker.internal:11434` (`DOCKER_LLM_BASE_URL` overrides it); every other LLM setting comes
   from `.env`, so a hosted provider still needs no code change.
4. **`make up` starts only the databases**, for `make api` / `make ui` natively during
   development. The two modes share ports 8000 and 8501, so only one runs at a time.
5. **`make seed`** loads a fresh, usable graph in one command: `ingest → er → er-write → qa-index
   → alerts`, with the tuned thresholds that are the config defaults. Evaluation splits and the LLM
   review are not part of it. Each step refuses to overwrite existing results, as before.
6. **CI builds the image** on every push, and the README shows the CI badge.

## Evidence

- `docker compose build` succeeds; the image is 3.76 GB, most of it CPU-only torch for the
  embedding model.
- `docker compose up -d --wait`: all four services healthy. `make smoke` passed against the
  containers (cited answer, then a labeled SAR draft through Ollama on the host). The containerized
  workbench showed the 1,560 alerts from the containerized API.
- `make seed` is a composition of targets that each already ran on this graph; it was not re-run
  from an empty graph, which would rewrite every ER edge and alert.

## Limits

- Raw data is still downloaded by hand (Kaggle needs an account), so `make seed` runs after that.
- The image is large; a slimmer API image would need the embedding step split out.
