.PHONY: seed up down logs api smoke test test-integration lint verify-data stage ingest graph-reset pattern-coverage leak-test er-split er er-tune er-adjudicate er-write er-reset er-test qa-index qa-check detect-split detect-tune detect-test alerts alerts-reset sar-check eval ui

# Start Neo4j and Qdrant only (for `make api` natively); --wait blocks until both are healthy.
# `docker compose up` starts everything, including the API and workbench containers.
up:
	@test -f .env || { echo "Missing .env: run 'cp .env.example .env' and set NEO4J_PASSWORD."; exit 1; }
	docker compose up -d --wait neo4j qdrant

# Stop the services. Data volumes are kept; 'docker compose down -v' would delete them.
down:
	docker compose down

logs:
	docker compose logs --tail=100

# The API runs natively, like Ollama: it needs the local embedding model and reaches Ollama on
# localhost (ADR 009). Needs `make up`.
api:
	uv run uvicorn tracegraph.api.app:app --host 127.0.0.1 --port 8000 --no-access-log

# Against the running stack (`make up`, `make api`, Ollama, `make alerts`): /health, then the
# README's /ask and /sar examples, which must be answered and drafted. Run before any commit that
# touches the API or the README examples.
SMOKE_QUESTION = How much did account 10:8000588D0 send between 1 and 5 September 2022?
SMOKE_ALERT = alert-0016267745f71997
smoke:
	curl -fsS --max-time 10 localhost:8000/health && echo
	curl -fsS --max-time 300 localhost:8000/ask -H 'content-type: application/json' \
	  -d '{"question": "$(SMOKE_QUESTION)"}' | uv run python -c "import json, sys; \
	r = json.load(sys.stdin); print(r['status'], '|', r['answer']); sys.exit(r['status'] != 'answered')"
	curl -fsS --max-time 300 localhost:8000/sar -H 'content-type: application/json' \
	  -d '{"alert_id": "$(SMOKE_ALERT)"}' | uv run python -c "import json, sys; \
	r = json.load(sys.stdin); print(r['status'], '|', r['text'][:300]); \
	sys.exit(r['status'] != 'drafted' or not r['text'].startswith('DRAFT'))"

test:
	uv run pytest

# Runs the integration tests against a throwaway Neo4j (port 7688, tmpfs), never the real graph.
test-integration:
	docker compose --profile test up -d --wait neo4j-test qdrant-test
	uv run pytest -m integration; status=$$?; docker compose --profile test rm -sf neo4j-test qdrant-test; exit $$status

lint:
	uv run ruff check src tests eval ui
	uv run ruff format --check src tests eval ui
	uv run mypy src ui

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

# Phase 2 (ADR 007). One-time dev/test split of customers (eval-only; never regenerated silently).
er-split:
	uv run python eval/er_split.py

# Score customers against source entities (no graph writes); needs `make up` for Qdrant.
er:
	uv run python -m tracegraph.er.run

# Tune thresholds and report every ER metric on the dev split.
er-tune:
	uv run python eval/er_eval.py --split dev

# Step B: the configured LLM (local Ollama by default) decides each review-band pair. Resumable.
er-adjudicate:
	uv run python -m tracegraph.er.adjudicator

# Write SAME_AS / POSSIBLE_SAME_AS edges with the configured (dev-tuned) thresholds.
er-write:
	uv run python -m tracegraph.er.writer

# Delete every ER edge (nothing else) after you type 'reset'.
er-reset:
	@read -p "This deletes ALL SAME_AS / POSSIBLE_SAME_AS edges. Type 'reset' to continue: " answer; \
	if [ "$$answer" = "reset" ]; then uv run python -m tracegraph.er.writer --reset --yes; else echo "Aborted."; fi

# Final numbers: score the test split ONCE with thresholds frozen from a dev report.
er-test:
	uv run python eval/er_eval.py --split test

# Phase 3: index customer, source-entity and bank names in Qdrant for entity linking.
qa-index:
	uv run python -m tracegraph.retrieval.vector_retriever

# Phase 3: questions generated from staging with gold answers, run through the full pipeline.
qa-check:
	uv run python eval/qa_check.py

# Phase 4 (ADR 010). One-time dev/test split of seed accounts (eval-only; never regenerated silently).
detect-split:
	uv run python eval/detection_split.py

# Tune detector thresholds on the dev split and report every metric.
detect-tune:
	uv run python eval/detection_eval.py --split dev

# Final numbers: score the test split ONCE with thresholds frozen from the latest dev report.
detect-test:
	uv run python eval/detection_eval.py --split test

# Write Alert nodes from the four detectors with the configured (dev-tuned) thresholds.
alerts:
	uv run python -m tracegraph.detection.alerts

# Delete every Alert node and its FLAGS edges (nothing else) after you type 'reset'.
alerts-reset:
	@read -p "This deletes ALL Alert nodes. Type 'reset' to continue: " answer; \
	if [ "$$answer" = "reset" ]; then uv run python -m tracegraph.detection.alerts --reset --yes; else echo "Aborted."; fi

# Draft SAR narratives for sampled alerts and report grounding and labeling.
sar-check:
	uv run python eval/sar_check.py

# Load a fresh graph and name index from staging, ready to use: graph, entity-resolution edges with
# the dev-tuned thresholds, Qdrant name index and typology alerts. Evaluation splits and the LLM
# review are not needed. Each step refuses to overwrite existing results (see its reset target).
seed: ingest er er-write qa-index alerts

# Collect every result file README.md and docs/RESULTS.md cite into one timestamped JSON + Markdown
# report. Nothing is re-run: held-out evaluations run once, through their own commands.
eval:
	uv run python eval/report.py

# Phase 5: Streamlit UI on localhost:8501; a thin client of the API (needs `make api`).
ui:
	uv run streamlit run ui/app.py --server.address 127.0.0.1 --server.headless true
