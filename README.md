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

## Typology alerts and SAR drafts

Four detectors look for laundering typologies in the payment graph:
- **Fan-in / fan-out:** many distinct counterparties within a time window.
- **Cycles:** time-ordered cycles of payments.
- **Scatter-gather:** one source paying several intermediates, each of which pays the same sink.

Their thresholds were tuned on a development half of the seed accounts and scored once on the
other half (see Results). `make alerts` writes the matches as `Alert` nodes linked to the flagged
accounts.

```sh
make alerts   # 1,560 alerts on the current stage; `make alerts-reset` removes them
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "Are there any typology alerts on account 19329:810E5B020?"}'
curl -s localhost:8000/sar -H 'content-type: application/json' \
  -d '{"alert_id": "alert-0016267745f71997"}'
```

A SAR draft narrates one alert from its evidence: the alert, the flagged accounts and their owners,
and the payments. Every sentence cites IDs and passes the same grounding check as answers. Every
draft starts with `DRAFT – requires analyst review`. Alerts are pattern matches in synthetic
transactions, not findings of wrongdoing.

`GET /alerts?subject=<account or customer ID>` or `GET /alerts?typology=CYCLE` lists alerts with
their summaries through a read-only template, without the LLM.

## Investigator UI

A Streamlit page on top of the API, with three tabs:
- **Ask:** a question, its cited answer sentences, the evidence (cited items first), and the route.
- **Alerts:** alerts on an account or customer, or one typology, with a button that sends an alert
  to the SAR tab.
- **SAR draft:** a grounded draft for one alert, shown with its label and downloadable as text.

```sh
make api   # in one terminal (needs `make up` and Ollama)
make ui    # in another; opens on http://127.0.0.1:8501
```

The page calls the API only. It never queries Neo4j or the LLM itself, so the read-only, grounding
and labeling guards stay in one place. `UI_API_URL` in `.env` points it at another API.

## Data and safety

Review [docs/DATA_LICENSES.md](docs/DATA_LICENSES.md) before downloading data. Synthetic links between IBM accounts and real OpenSanctions/ICIJ names are not evidence of wrongdoing. ICIJ inclusion does not imply wrongdoing. SAR narratives are labeled `DRAFT – requires analyst review`.

## Results

Every number below comes from an evaluation command. The cited result files are committed in
`eval/results/`, with the LLM provider and model recorded in each. `make eval` collects every
result file this README cites into one timestamped JSON and Markdown report, and fails if one is
missing. It re-runs nothing, because each held-out evaluation is run once through its own command.

### Question answering (`make qa-check`, held-out set)

These results use the local `qwen2.5:7b-instruct` model through Ollama, with
`BAAI/bge-small-en-v1.5` name embeddings, on an M2 laptop. There are 60 questions generated from
staging (question seed 44), with gold answers computed from the staged Parquet files, never from
the graph. Development and fixes used a separate seed-42 set; this set was run once, with no
tuning afterwards. [Results file](eval/results/qa_check_20261115T072915Z.json).

| Measure | Result | 95% CI (Wilson) |
| --- | --- | --- |
| Answer accuracy | 47/60 (0.78) | 0.66–0.87 |
| Router intent accuracy | 50/60 (0.83) | 0.72–0.91 |
| Linking accuracy (questions naming a party) | 50/50 (1.00) | 0.93–1.00 |
| Account holdings | 9/10 | 0.60–0.98 |
| Transaction totals (per currency) | 9/10 | 0.60–0.98 |
| Payment paths (5 connected, 5 not) | 10/10 | 0.72–1.00 |
| ICIJ entity networks | 10/10 | 0.72–1.00 |
| Typology alerts on an account (5 flagged, 5 not) | 9/10 | 0.60–0.98 |
| Lists across customers (text-to-Cypher) | 0/10 | 0.00–0.28 |

The outcomes were 50 answered, 9 clarifying questions and 1 "not found". One answer needed a
regeneration to pass the grounding check. Mean latency was 12.6 s per question (p95 27.5 s).

Known failures and limits:
- **List questions failed.** The router sends them to a single-customer template instead of
  text-to-Cypher, so all 10 ended as a clarifying question or "not found".
- **The other three misses were incomplete answers, not wrong facts.**
  - A total left out one of two currencies.
  - An alerts answer cited one of an account's two alerts.
  - An owner was named without their customer ID.
- **Watchlist questions are not evaluated yet**: the graph has no `SAME_AS` edges until entity
  resolution writes them (Phase 2).
- **Per-family results rest on 10 questions each.**

An earlier held-out run, seed 43, came before the alerts intent was added. It scored 40/50 on the
five families it had, with lists 0/10, and two list questions were answered with grounded facts
about the wrong customer ([results file](eval/results/qa_check_20261115T051015Z.json)).

### Typology detection (`make detect-test`, held-out seed accounts)

The test half of the seed accounts was scored once, with thresholds frozen from the development
half: 7,178 accounts, of which 44.3% are core seeds (they touch a laundering transaction).
- **Precision** is the share of flagged accounts that are core seeds. The sample enriches
  laundering about 19× compared with the full dataset (0.10% of transactions), so this is not
  real-world precision.
- **Pattern recall** is the share of IBM's labeled patterns of that type where the detector flags
  the right account (the hub, a cycle member, or the scatter-gather source or sink).
- **Degree baseline:** flag the same number of accounts by raw transaction count. The last column
  gives the detector minus the baseline, as a paired bootstrap 95% CI.

[Results file](eval/results/detection_test_final.json).

| Detector | Pattern recall | Precision | Degree baseline (recall, precision) | Detector − baseline, 95% CI |
| --- | --- | --- | --- | --- |
| Fan-in (≥6 senders, 168 h) | 15/22 (0.68) | 0.79 | 10/22, 0.53 | precision +0.17 to +0.36; recall −0.12 to +0.69 |
| Fan-out (≥7 receivers, 96 h) | 11/27 (0.41) | 0.57 | 5/27, 0.51 | precision −0.02 to +0.13; recall −0.09 to +0.60 |
| Cycle (≤12 hops, 96 h) | 23/24 (0.96) | 0.89 | 17/24, 0.84 | precision −0.00 to +0.11; recall +0.03 to +0.53 |
| Scatter-gather (≥3 intermediates, 96 h) | 16/17 (0.94) | 0.98 | 3/17, 0.81 | precision +0.03 to +0.32; recall +0.46 to +1.00 |

- **Scatter-gather beats the degree baseline on both measures,** fan-in on precision, and cycles
  on recall.
- **Fan-out does not clearly beat it.** Its recall fell from 16/21 on development to 11/27 on test,
  so its thresholds fit the development data too closely.
- **With 17–27 patterns per type, the intervals are wide.**

### SAR drafts (`make sar-check`)

Twenty alerts were sampled, five per typology, and drafted with `qwen2.5:7b-instruct`.
[Results file](eval/results/sar_check_20261115T075915Z.json).
- **16 of 20 produced a grounded draft**: 15 on the first try and 1 after one regeneration.
- **4 abstained**: 2 scatter-gather, 1 fan-in, and 1 fan-out whose reply was not valid JSON.
  Abstaining means saying "no grounded narrative" instead of returning an ungrounded draft.
- **Every draft carries `DRAFT – requires analyst review`**, and none uses accusatory words
  (launder, fraud, crime, illegal, illicit, guilt).
- **Mean latency was 90 s per draft** on the local model.

The grounding check confirms that cited IDs and numbers come from the evidence. It cannot tell
whether a number was read correctly: an early draft called a detection threshold "6 sender
accounts". Alerts now carry a code-written summary of what they count (ADR 010).

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
