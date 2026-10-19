# ADR 001: Local service orchestration

- **Status:** Accepted (2026-10-19). Docker was deferred until the user enabled it for Phase 1 Step 3
- **Decision:** Run Neo4j 5.26 (with APOC and GDS), Qdrant, and the FastAPI app through Docker Compose. `make up` runs `docker compose up -d --build --wait`, and `make down` stops the services but keeps the data volumes.
- **Rationale:** Loading the graph (Phase 1 Step 3) and the Neo4j integration tests need a running database. The user first chose to skip Docker, then enabled it for Step 3.
- **Consequences:** Every port binds to 127.0.0.1 only. `.env` (gitignored) must set `NEO4J_PASSWORD`. Qdrant runs, but stays empty until Phase 2.
