.PHONY: up down logs test lint verify-data stage ingest er eval

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

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src

# Phase 1 Step 0: check manually downloaded raw data (headers, checksums, manifests).
verify-data:
	uv run python -m tracegraph.ingest.verify_data

# Phase 1 Steps 1-2: parse, sample, synthesize, validate, and write staging Parquet (no database).
stage: verify-data
	uv run python -m tracegraph.ingest.stage

ingest: stage
	@echo "Graph load (Phase 1 Step 3) not implemented yet; staging files are in data/processed/staging/."

er:
	@echo "not implemented"

eval:
	@echo "not implemented"
