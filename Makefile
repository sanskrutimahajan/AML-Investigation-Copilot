.PHONY: up down test lint ingest er eval

up:
	@echo "Docker startup is deferred; no containers were started."

down:
	@echo "Docker shutdown is deferred; no containers were changed."

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run mypy src

ingest:
	@echo "not implemented"

er:
	@echo "not implemented"

eval:
	@echo "not implemented"
