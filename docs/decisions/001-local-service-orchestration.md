# ADR 001: Local service orchestration

- **Status:** Deferred
- **Decision:** Keep the Neo4j 5, Qdrant, and FastAPI Compose configuration available, but do not run it until the user asks. `make up` and `make down` are non-operational informational targets for now.
- **Rationale:** The user requested skipping Docker execution in the current setup phase.
- **Consequences:** The API health endpoint requires independently available Neo4j and Qdrant services and reports HTTP 503 if either cannot be reached. No containers are started or stopped by the current Make targets.
