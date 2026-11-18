# Setup and usage

How to install, build the graph, and run the [AML Investigation Copilot](../README.md).

## Setup

Requirements: Python 3.11, [`uv`](https://docs.astral.sh/uv/), Docker Desktop (allocate at least
6 GB of memory) and [Ollama](https://ollama.com).

```sh
uv sync
cp .env.example .env   # set NEO4J_PASSWORD; the LLM defaults to local Ollama
make test              # unit tests; need neither .env nor running services
make lint              # Ruff and mypy
```

### Services

```sh
make up      # Neo4j 5.26 + APOC/GDS and Qdrant in Docker; waits until both are healthy
make api     # the FastAPI service, natively on 127.0.0.1:8000 (another terminal)
curl localhost:8000/health    # {"status":"healthy","services":{"neo4j":"healthy","qdrant":"healthy"}}
make down    # stop Neo4j and Qdrant; data volumes are kept
make test-integration         # integration tests against throwaway Neo4j/Qdrant containers
```

The API runs natively, like Ollama: it embeds names with the local model at query time and
reaches Ollama on `localhost`. All ports bind to 127.0.0.1. The Neo4j Browser is at
http://localhost:7474 (user `neo4j`, password from `.env`).

### LLM (local by default)

Every LLM step (query routing, ER adjudication, text-to-Cypher, answer generation and SAR drafts)
runs on a **local model through Ollama** by default. Ollama runs natively on macOS, outside
Docker, because Docker on a Mac has no GPU access:

```sh
ollama pull qwen2.5:7b-instruct   # about 4.7 GB; pick a ~3B model if you have less than 16 GB RAM
```

Never commit `.env`, an API key, or downloaded data.

### Using a different LLM

The provider is chosen in `.env` only, with no code changes. To switch from Ollama to a hosted API,
set two lines (plus the model), for example OpenAI:

```sh
LLM_PROVIDER=openai_compatible        # or: anthropic
LLM_API_KEY=sk-...                    # then set LLM_BASE_URL / LLM_MODEL for that provider
```

Groq, Together, vLLM and LM Studio work the same way (`openai_compatible` plus their
`LLM_BASE_URL`); `.env.example` has a commented example for each. Every evaluation results file
records the provider and model it used.

## Building the graph

Download the raw data by hand into `data/raw/` (sources, licenses and checksums:
[docs/DATA_LICENSES.md](DATA_LICENSES.md)). Then, with `make up` running:

```sh
make ingest          # verify-data and stage first, then load and verify the Neo4j graph
make er-split        # one-time dev/test split of customers
make er              # block and score customer ↔ source-record pairs (no graph writes)
make er-tune         # tune thresholds on dev; the tuned values are the defaults in .env.example
make er-adjudicate   # local LLM review of the review band (resumable; hours on a laptop)
make er-test         # score the held-out split once
make er-write        # write SAME_AS / POSSIBLE_SAME_AS edges
make qa-index        # index customer, source-record and bank names in Qdrant
make alerts          # write Alert nodes with the tuned detector thresholds
```

`make ingest` is idempotent and fails loudly on a stale graph; `make graph-reset`, `make er-reset`
and `make alerts-reset` are the only ways to delete, and each asks you to type `reset`. The row
counts from each staging run are written to `data/processed/staging/stage_manifest.json`.
`make er-adjudicate` is needed only for the reported LLM comparison: the LLM's decisions change no
edge (`ER_USE_LLM_ADJUDICATION=false`). The detector split and tuning (`make detect-split`,
`make detect-tune`, `make detect-test`) are described in [RESULTS.md](RESULTS.md).

## Asking questions

With the graph built and `make api` running:

```sh
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "How much did account 10:8000588D0 send between 1 and 5 September 2022?"}'
make smoke   # /health, this /ask example and the /sar example below must all succeed
```

`make qa-check` runs 60 questions generated from staging (10 per family), with gold answers,
through the whole pipeline. `uv run python eval/qa_check.py --seed N` runs a different question set.

## Alerts and SAR drafts

`make alerts` wrote 1,560 alerts on the current stage. For example:

```sh
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Are there any typology alerts on account 19329:810E5B020?"}'
curl -s localhost:8000/sar -H 'content-type: application/json' \
  -d '{"alert_id": "alert-0016267745f71997"}'
```

`GET /alerts?subject=<account or customer ID>` or `GET /alerts?typology=CYCLE` lists alerts with
their summaries through a read-only template, without the LLM.

## Investigator workbench

A Streamlit workbench on top of the API, laid out for an alert analyst's day:

| Section | Page | What it does |
| --- | --- | --- |
| Monitoring | **Overview** | Open alerts per typology, typology coverage, and the latest alerts. |
| Monitoring | **Alert queue** | Alerts newest first, filtered by typology or by customer/account ID and paged 25 at a time. Selecting one opens a review panel with its summary, thresholds, flagged accounts and owners, and its payments. From there you can open the owner's profile or draft a SAR. |
| Investigation | **Subject profile** | One customer, looked up by customer ID, account ID or name. Shows the KYC record, accounts and banks, payment counts (all accounts), activity by account and currency, largest payments, alerts, and links to sanctions or offshore records. |
| Investigation | **Name screening** | Fuzzy full-text and semantic name search across OpenSanctions, ICIJ Offshore Leaks and customer records, with each candidate's record. |
| Investigation | **Investigation assistant** | A question in plain language, answered with sentences that cite graph IDs, plus the evidence and the route. |
| Reporting | **SAR drafting** | Load an alert's evidence, then draft a grounded narrative. It is shown under the `DRAFT – requires analyst review` label and can be downloaded as text. |

```sh
make api   # in one terminal (needs `make up` and Ollama)
make ui    # in another; opens on http://127.0.0.1:8501
```

The theme (navy and slate, IBM Plex type) is in `.streamlit/config.toml`.

The pages other than the assistant and SAR drafting read through these API endpoints, each backed by a fixed, read-only Cypher template:
- `GET /alerts/summary`
- `GET /alerts/queue`
- `GET /alerts/{alert_id}`
- `GET /subjects/{id}`
- `GET /screen?name=`

`UI_API_URL` in `.env` points the workbench at another API.

Before any commit that touches the API or these examples, `make smoke` must pass.
