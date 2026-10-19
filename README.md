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
cp .env.example .env   # set NEO4J_PASSWORD; leave LLM_ENABLED=false
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
make up                       # Neo4j 5.26 + APOC/GDS, Qdrant, API; waits until all are healthy
curl localhost:8000/health    # {"status":"healthy","services":{"neo4j":"healthy","qdrant":"healthy"}}
make down                     # stop the services; data volumes are kept
```

All ports bind to 127.0.0.1. The Neo4j Browser is at http://localhost:7474 (user `neo4j`, password from `.env`). The test and lint commands need neither `.env` nor running services.

LLM calls are disabled by default (`LLM_ENABLED=false`). Provider clients are created lazily, only on a call when explicitly enabled and the corresponding key is configured. No provider is called by the API or test suite. Do not enable LLM calls or add/connect an API key without first obtaining explicit approval. Never commit `.env` or downloaded data.

## Data and safety

Review [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md) before downloading data. Synthetic links between IBM accounts and real OpenSanctions/ICIJ names are not evidence of wrongdoing. ICIJ inclusion does not imply wrongdoing. SAR narratives will be labeled `DRAFT – requires analyst review`.

## Results

Question answering and detection results are pending evaluation.

**Synthetic-data leakage check** (`make leak-test`, run 2026-10-19). Can a classifier spot seeded
customers from the customer's own fields alone, without comparing them to any watchlist record?
Five-fold cross-validated ROC-AUC (0.5 = chance; the target is ≤ 0.60):

| Comparison | Logistic regression | Gradient boosting |
| --- | --- | --- |
| Seeds vs plain customers | 0.527 | 0.590 |
| Seeds vs look-alikes | 0.526 | 0.547 |
| Seeds + look-alikes vs plain | 0.514 | 0.569 |

There is some residual leakage: AUC 0.590 at most. This is the baseline entity resolution has to beat
([ADR 006](docs/decisions/006-seed-independent-customers-and-icij-addresses.md)).
