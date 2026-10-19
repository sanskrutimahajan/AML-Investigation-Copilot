.PHONY: up down logs test test-integration lint verify-data stage ingest graph-reset pattern-coverage leak-test er eval

# Start Neo4j, Qdrant and the API; --wait blocks until every healthcheck passes.
up:
	@test -f .env || { echo "Missing .env: run 'cp .env.example .env' and set NEO4J_PASSWORD."; exit 1; }
	docker compose up -d --build --wait

# Stop the services. Data volumes are kept; 'docker compose down -v' would delete them.
down:
	docker compose down

logs:
	docker compose logs --tail=100

test:
	uv run pytest

# Runs the integration tests against a throwaway Neo4j (port 7688, tmpfs), never the real graph.
test-integration:
	docker compose --profile test up -d --wait neo4j-test
	uv run pytest -m integration; status=$$?; docker compose --profile test rm -sf neo4j-test; exit $$status

lint:
	uv run ruff check src tests eval
	uv run ruff format --check src tests eval
	uv run mypy src

# Phase 1 Step 0: check manually downloaded raw data (headers, checksums, manifests).
verify-data:
	uv run python -m tracegraph.ingest.verify_data

# Phase 1 Steps 1-2: parse, sample, synthesize, validate, and write staging Parquet (no database).
stage: verify-data
	uv run python -m tracegraph.ingest.stage

# Phase 1 Step 3: validate staging and load it into Neo4j (needs `make up`). Idempotent.
ingest: stage
	uv run python -m tracegraph.graph.loaders

# Deletes every node and relationship in the main graph after you type 'reset'. Schema is kept.
graph-reset:
	@read -p "This deletes ALL graph data in Neo4j (schema is kept). Type 'reset' to continue: " answer; \
	if [ "$$answer" = "reset" ]; then uv run python -m tracegraph.graph.reset --yes; else echo "Aborted."; fi

# Eval-side report: how IBM pattern transactions map to loaded SENT edges (reads ground truth).
pattern-coverage:
	uv run python eval/pattern_coverage.py

# Eval-side: can customer-only fields reveal seed status? Fails if any ROC-AUC exceeds 0.60.
leak-test:
	uv run python eval/leak_test.py

er:
	@echo "not implemented"

eval:
	@echo "not implemented"
