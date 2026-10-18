# AML Investigation Copilot

AML Investigation Copilot is a Graph-RAG assistant for AML investigations. It is designed to answer analyst questions using a Neo4j knowledge graph and Qdrant retrieval, with factual claims grounded in cited source IDs. This is a portfolio prototype, not an automated determination of wrongdoing.

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
make test
make lint
```

Docker Compose setup is present but deferred: `make up` and `make down` do not start or stop containers. The FastAPI health endpoint requires independently available Neo4j and Qdrant services and returns HTTP 503 when either is unavailable. No `.env` file, API key, or external service is needed to run the test and lint commands.

LLM calls are disabled by default (`LLM_ENABLED=false`). Provider clients are created lazily, only on a call when explicitly enabled and the corresponding key is configured. No provider is called by the API or test suite. Do not enable LLM calls or add/connect an API key without first obtaining explicit approval. Never commit `.env` or downloaded data.

## Data and safety

Review [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md) before downloading data. Synthetic links between IBM accounts and real OpenSanctions/ICIJ names are not evidence of wrongdoing. ICIJ inclusion does not imply wrongdoing. SAR narratives will be labeled `DRAFT – requires analyst review`.

## Results

pending evaluation
