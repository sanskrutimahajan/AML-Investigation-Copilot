.PHONY: up down test lint verify-data stage ingest er eval

up:
	@echo "Docker startup is deferred; no containers were started."

down:
	@echo "Docker shutdown is deferred; no containers were changed."

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
