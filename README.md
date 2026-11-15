# AML Investigation Copilot

AML Investigation Copilot is a Graph-RAG assistant for AML investigations. It is designed to answer analyst questions using a Neo4j knowledge graph and Qdrant retrieval, with factual claims grounded in cited source IDs. 

## Problem

Investigators need to connect transaction activity with contextual entity records without losing the ability to verify where an answer came from. The copilot combines graph retrieval, vector search, entity resolution, and cited answer generation while clearly distinguishing synthetic test links from source data.

## Architecture

```text
IBM AML HI-Small ─┐
OpenSanctions ────┼─> Ingestion / normalization ─> Entity resolution ─> Neo4j
ICIJ Offshore ────┘                                                         │
                                                                            v
Analyst ─> FastAPI ─> routing / entity linking ─> Qdrant + guarded graph retrieval
                                                        │
                                                        v
                                              evidence fusion / LLM
                                                        │
                                                        v
                                           cited answer + grounding check
```

The architecture and planned phases are described in [docs/PROPOSAL.md](docs/PROPOSAL.md).

## Setup

Requirements: Python 3.11 and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
cp .env.example .env   # set NEO4J_PASSWORD; the LLM defaults to local Ollama
make test
make lint
```

### Data

Download the raw data by hand into `data/raw/` (sources and licenses: [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md)), then run:

```sh
make verify-data   # check headers and checksums against data/raw/*/MANIFEST.tsv
make stage         # parse, sample, synthesize, validate -> data/processed/staging/
make ingest        # stage, then load and verify the Neo4j graph (needs `make up`)
```

The row counts from each staging run are written to `data/processed/staging/stage_manifest.json`.

### Services

Requires Docker Desktop (allocate at least 6 GB of memory).

```sh
make up      # Neo4j 5.26 + APOC/GDS and Qdrant in Docker; waits until both are healthy
make api     # the FastAPI service, natively on 127.0.0.1:8000 (another terminal)
curl localhost:8000/health    # {"status":"healthy","services":{"neo4j":"healthy","qdrant":"healthy"}}
make down    # stop Neo4j and Qdrant; data volumes are kept
```

The API runs natively, like Ollama: it embeds names with the local model at query time and
reaches Ollama on `localhost`. All ports bind to 127.0.0.1. The Neo4j Browser is at http://localhost:7474 (user `neo4j`, password from `.env`). The test and lint commands need neither `.env` nor running services.

### LLM (local by default)

Every LLM step (query routing, ER adjudication, text-to-Cypher, answer generation and SAR drafts) runs on a **local model through [Ollama](https://ollama.com)** by default. No API key is needed and nothing is billed. Ollama runs natively on macOS, outside Docker, because Docker on a Mac has no GPU access:

```sh
ollama pull qwen2.5:7b-instruct   # about 4.7 GB; pick a ~3B model if you have less than 16 GB RAM
```

Never commit `.env`, an API key, or downloaded data.

### Using a different LLM

The provider is chosen in `.env` only, with no code changes. To switch from Ollama to a hosted API, set two lines (plus the model), for example OpenAI:

```sh
LLM_PROVIDER=openai_compatible        # or: anthropic
LLM_API_KEY=sk-...                    # then set LLM_BASE_URL / LLM_MODEL for that provider
```

Groq, Together, vLLM and LM Studio work the same way (`openai_compatible` plus their `LLM_BASE_URL`); `.env.example` has a commented example for each. Every evaluation results file records the provider and model it used.

## Asking questions

After `make ingest`, index the names used to match customers, entities and banks in a question
(about a minute; it reuses the ER embedding cache), then ask through the running API:

```sh
make qa-index
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "How much did account 10:8000588D0 send between 1 and 5 September 2022?"}'
make smoke   # /health, then this /ask example must come back answered
```

The question is routed to one of five Cypher templates (customer profile, transactions, payment
path, watchlist links, source-entity network), or to guarded text-to-Cypher for anything else.
Every query runs read-only, with a row limit, a hop limit and a timeout. Generated Cypher must
also pass `EXPLAIN`, which checks that it is read-only and names only known labels, relationship
types and properties. Each answer sentence cites graph IDs. An answer whose citations or numbers
are not in the retrieved evidence is regenerated once, then replaced by "insufficient evidence".
A name that matches several records gets a clarifying question back instead of a guess. Answers
that touch customer or ICIJ data carry fixed notices: the links are synthetic, and ICIJ inclusion
does not imply wrongdoing.

`make qa-check` runs 50 questions generated from staging, with gold answers, through the whole
pipeline. `uv run python eval/qa_check.py --seed N` runs a different question set.

## Data and safety

Review [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md) before downloading data. Synthetic links between IBM accounts and real OpenSanctions/ICIJ names are not evidence of wrongdoing. ICIJ inclusion does not imply wrongdoing. SAR narratives will be labeled `DRAFT – requires analyst review`.

## Results

Every number below comes from an evaluation command. The cited result files are committed in
`eval/results/`, with the LLM provider and model recorded in each.

### Question answering (`make qa-check`, held-out set)

These results use the local `qwen2.5:7b-instruct` model through Ollama, with `BAAI/bge-small-en-v1.5`
name embeddings, on an M2 laptop. There are 50 questions generated from staging (question seed 43),
with gold answers computed from the staged Parquet files, never from the graph. Development and
fixes used a separate seed-42 set; this set was run once afterwards, with no tuning.
[Results file](eval/results/qa_check_20261115T051015Z.json).

| Measure | Result | 95% CI (Wilson) |
| --- | --- | --- |
| Answer accuracy | 40/50 (0.80) | 0.67–0.89 |
| Router intent accuracy | 40/50 (0.80) | 0.67–0.89 |
| Linking accuracy (questions naming a party) | 40/40 (1.00) | 0.91–1.00 |
| Account holdings | 10/10 | 0.72–1.00 |
| Transaction totals (per currency) | 10/10 | 0.72–1.00 |
| Payment paths (5 connected, 5 not) | 10/10 | 0.72–1.00 |
| ICIJ entity networks | 10/10 | 0.72–1.00 |
| Lists across customers (text-to-Cypher) | 0/10 | 0.00–0.28 |

The outcomes were 42 answered, 7 clarifying questions and 1 "not found". No answer needed a
regeneration to pass the grounding check. Mean latency was 16.9 s per question (p95 41.0 s).

Known failures and limits:
- **List questions failed.** The router sent all 10 to the single-customer template instead of
  text-to-Cypher. Eight ended as a clarifying question or "not found".
  - Two were **answered with grounded but irrelevant facts** about a customer whose name resembled
    the description, e.g. "Ml Isvest" for country code "ML".
  - This failure mode is reported here, not tuned away after the held-out run.
- **Watchlist questions are not evaluated yet**: the graph has no `SAME_AS` edges until entity
  resolution writes them (Phase 2).
- **Per-family results rest on 10 questions each**, so their intervals are wide.

### Synthetic-data leakage check (`make leak-test`)

Can a classifier tell seeded customers apart using only the customer's own fields (name, DOB,
country, address)? Each score is a 5-fold cross-validated ROC-AUC, where 0.50 is chance; the
target is 0.60 or below. This is one run on the current stage, after the address-determinism fix.
[Results file](eval/results/leak_test_20261115T060250Z.json).

| Comparison | Logistic regression | Gradient boosting |
| --- | --- | --- |
| Seeds vs plain | 0.522 | 0.569 |
| Look-alikes vs plain | 0.503 | 0.494 |
| Homonyms vs plain | 0.583 | **0.621** |
| Seeds vs homonyms | 0.561 | 0.550 |
| Seeds vs look-alikes | 0.503 | 0.536 |
| Seeds + look-alikes vs plain | 0.523 | 0.553 |

**The maximum, 0.621 (homonyms vs plain, gradient boosting), is above the 0.60 target, so the
script reports FAIL.** This is a disclosed judgment call, accepted on 2026-11-15 on the condition
that no score exceeds 0.625:
- By design, homonyms carry a real record's exact name with a conflicting DOB or country.
- Their name style and the conflicting-country rule leave a small signal. On their own, the name
  n-grams score 0.597 and the categorical fields 0.575.
- Every comparison involving seeds, which entity resolution must find, is at or below 0.569.

Entity resolution has to beat these scores, and its homonym false-positive rate is reported
separately (Phase 2, pending).
