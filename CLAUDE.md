# AML Investigation Copilot

## Project

AML Investigation Copilot is a Graph-RAG assistant for anti-money-laundering investigations. Analysts ask natural-language questions about customers, accounts, and transactions; answers are grounded in a Neo4j graph plus Qdrant vector retrieval, and factual claims cite node or edge IDs. See [docs/PROPOSAL.md](docs/PROPOSAL.md).

This is a portfolio project. Correct, honest, measurable results matter more than feature count.

## Capabilities (build in this order)

1. Ingestion and graph build (IBM AML HI-Small, OpenSanctions subset, ICIJ Offshore Leaks subset)
2. Entity resolution (Splink + embeddings, LLM adjudication for gray-zone pairs) → `SAME_AS` edges
3. Retrieval and Q&A: router → entity linking → Cypher templates / guarded text2Cypher → fusion → cited answer
4. Typology detection (fan-in, fan-out, cycles, scatter-gather) → `Alert` nodes; SAR narrative drafter
5. Evaluation harness + Streamlit UI

## Stack

- Python 3.11, `uv`, Polars
- Neo4j 5 (Docker, APOC + GDS), official `neo4j` Python driver
- Qdrant (Docker), `qdrant-client`; embeddings via `sentence-transformers` (default `BAAI/bge-small-en-v1.5`)
- Splink, `rapidfuzz`, `unidecode`, `jellyfish`
- LLM interface in `src/tracegraph/llm/` (Anthropic API, OpenAI-compatible endpoint for vLLM)
- FastAPI service, Streamlit UI, RAGAS + custom evaluation metrics
- Docker Compose, pytest, Ruff, mypy, GitHub Actions CI

## Repository layout

```text
src/tracegraph/
  config.py
  ingest/       # ibm_aml.py, opensanctions.py, icij.py, synthetic_profiles.py, seed_links.py
  graph/        # schema.py, loaders.py, cypher_templates.py
  er/           # normalize.py, blocking.py, splink_model.py, adjudicator.py
  retrieval/    # router.py, entity_linking.py, graph_retriever.py, text2cypher.py, vector_retriever.py, fusion.py
  generation/   # answer.py, grounding_check.py, sar_drafter.py
  detection/    # patterns.py, alerts.py
  llm/          # base.py, anthropic_provider.py, openai_compat_provider.py
  api/
eval/
ui/
data/raw/ data/processed/  # gitignored
docs/                       # PROPOSAL.md, DATA_LICENSES.md, decisions/
```

## Commands

- `make up` / `make down` — start/stop Neo4j, Qdrant, API via Docker Compose (volumes persist)
- `make verify-data` — check manually downloaded raw data (headers, checksums) against `data/raw/*/MANIFEST.tsv`
- `make stage` — Phase 1 Steps 1–2: parse, sample, synthesize, validate, write `data/processed/staging/` (no database)
- `make ingest` — stage, then load and verify the Neo4j graph (idempotent; fails loudly on a stale graph)
- `make graph-reset` — wipe graph data after typing `reset` (schema kept)
- `make test-integration` — integration tests against the throwaway Neo4j on port 7688
- `make pattern-coverage` — eval-side report of IBM pattern transactions loaded as `SENT` edges
- `make er` — run entity resolution
- `make test` — pytest; `make lint` — Ruff + mypy
- `make eval` — run evaluation and write timestamped JSON + Markdown results

## Hard rules

- Never commit secrets. Read keys from `.env` (gitignored); keep `.env.example` updated.
- Do not add, connect, or use API keys or call paid external APIs without explicit user approval. LLM calls default to disabled.
- Never commit data. `data/` is gitignored. Download scripts must document source URL and license.
- Cypher from user/LLM input must run in a read-only session, be validated against schema, have a mandatory `LIMIT`, max hop count, and timeout. Templates use parameters, never string interpolation.
- Answers cite IDs. `grounding_check.py` verifies every cited ID exists in retrieved context; on failure regenerate once, then return “insufficient evidence.”
- Never fabricate metrics in README, docs, or comments. Report only numbers produced by `make eval`.
- Links between IBM accounts and real OpenSanctions/ICIJ names are synthetic. Never assert that a real person committed a crime. ICIJ inclusion does not imply wrongdoing.
- SAR output is always labeled `DRAFT – requires analyst review`.
- Configurable thresholds, model names, and sample sizes live in `config.py`, not scattered constants.

## Working style

- Before each phase, read `docs/PROPOSAL.md`, propose a short plan (files, functions, tests), and wait for approval.
- Docker and Docker Compose are deferred until the user explicitly asks to use them.
- Work in small, reviewable steps, one phase at a time. Stop at each phase end with a summary, run instructions, and open questions.
- Write tests alongside code, including integration tests against Dockerized Neo4j with a tiny fixture graph.
- Record notable decisions as short ADRs in `docs/decisions/NNN-title.md`.
- Prefer simple, readable code with type hints. If a proposal assumption is infeasible or a dataset differs from its description, explain the issue and propose an alternative.

## Decisions

Approved decisions are recorded as ADRs in [docs/decisions/](docs/decisions/). They are settled, so do not re-ask about them.

- [001](docs/decisions/001-local-service-orchestration.md): Docker Compose is present but deferred.
- [002](docs/decisions/002-defer-icij.md): ICIJ deferral, now superseded (ICIJ is in scope).
- [005](docs/decisions/005-sampling-bias-and-detection-evaluation.md): the IBM sample is biased (laundering enriched about 19×). Phase 4 must be evaluated on seed accounts only, report base rates, and beat a degree baseline.
- [004](docs/decisions/004-graph-load.md): Graph load:
  - Loads only from staging, with fixed relationship-type lists.
  - Fails loudly on a mismatch, and `make graph-reset` is the only way to delete.
  - Tests run against a throwaway test DB.
- [003](docs/decisions/003-phase1-ingestion-decisions.md): Phase 1 decisions:
  - Dependencies: polars, faker, pandera.
  - Laundering labels stay off the graph.
  - Data is downloaded manually and checked with `verify_data`.
  - Sampling strategy, caps, and the seeding split.
  - Docker deferred; Qdrant deferred to Phase 2.

## Data notes

- IBM AML: start with HI-Small (transactions CSV + patterns file). Model transactions as `SENT` edges.
- OpenSanctions: FollowTheMoney JSON; filter to `Person`/`Company`/`Organization` and a subset of datasets.
- ICIJ Offshore Leaks: CSV nodes (entities, officers, intermediaries, addresses, others) + relationships. ODbL share-alike.
- Seed links: approximately 2–5% of synthetic customers get names derived from watchlist/offshore records with perturbations (typos, transliteration, reordering, suffix removal, aliases). Store mapping in `data/processed/seed_ground_truth.parquet`.
- Confirm actual dataset formats, licenses, and sizes from primary sources before implementation; do not infer uncertain details.
