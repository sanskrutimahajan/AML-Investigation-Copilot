# TraceGraph audit: production-shaped AML copilot (Phase 0)

Audited 2026-11-18 on branch `phase2-er` (commit `98cd979`). No code was changed for this audit.
Scores: **Done**, **Partial**, **Missing**. File references are relative to the repository root.

## Summary

The core is strong and measured: a Neo4j graph built from IBM AML, OpenSanctions and ICIJ data;
entity resolution with held-out scores; four typology detectors scored against a baseline; cited,
grounded Q&A and SAR drafts; a read-only FastAPI service; and a Streamlit workbench. The biggest
gaps for a "tool a financial-crimes team could adopt" are at the end of the workflow: there are no
**cases**, no **disposition**, and no **audit trail of analyst actions**. The SAR draft cannot be
edited, and the case view has no graph picture. Typology coverage stops at four patterns,
`docker compose up` starts only the databases, and observability is plain console logging.

| # | Item | Score |
| --- | --- | --- |
| 1 | Investigator workflow end to end | Partial |
| 2 | Typology coverage | Partial |
| 3 | Real reference data mixed with synthetic, provenance labeled | Partial |
| 4 | Service layer | Partial |
| 5 | Reproducibility | Partial |
| 6 | Tests and CI | Partial |
| 7 | Evaluation harness | Partial |
| 8 | Explainability and audit trail | Partial |
| 9 | Guardrails | Partial |
| 10 | Observability | Partial (weak) |
| 11 | Data quality | Partial |
| 12 | Simulated live feed | Missing |
| 13 | Analyst UI | Partial |
| 14 | Demo mode | Missing |
| 15 | README as a design doc | Partial |

## Conflicts with settled decisions

Several requests in the brief reverse decisions recorded as ADRs. They need your call before the
affected phase starts (see [Decisions needed](#decisions-needed)).

| Brief asks for | Current decision | Why it was decided that way |
| --- | --- | --- |
| SAR quality via RAGAS | RAGAS dropped; gold-answer evaluation ([ADR 009](docs/decisions/009-retrieval-and-qa.md)) | RAGAS scores come from an LLM judge; with a local 7B judge they are noisy and hard to reproduce. Gold answers give exact scores |
| Everything via `docker compose up` | Neo4j and Qdrant in Docker; API and Ollama native ([ADR 001](docs/decisions/001-local-service-orchestration.md), [ADR 009](docs/decisions/009-retrieval-and-qa.md)) | Docker on macOS has no GPU, so Ollama must run natively; the API was moved native with it |
| A LangGraph agent in the architecture | A fixed pipeline: router → template or guarded text-to-Cypher → answer → grounding check | A deterministic pipeline is easier to test and guard; the brief also says "no over-engineering" |
| Each typology as a Cypher pattern | Detectors read `SENT` edges from Neo4j and match in Polars (`src/tracegraph/detection/patterns.py`) | Windowed cycle and scatter-gather search over 270k edges is faster and simpler in Polars |
| Analyst actions written to the store | Every Q&A query runs read-only ([ADR 009](docs/decisions/009-retrieval-and-qa.md)) | Not a conflict if writes go through separate, fixed endpoints; LLM-generated Cypher stays read-only |

## Checklist

### 1. Investigator workflow end to end: Partial

| Step | Score | Where |
| --- | --- | --- |
| Alert intake | Done | Four detectors write `Alert` nodes (`src/tracegraph/detection/alerts.py`); queue at `GET /alerts/queue`, `ui/views/alert_queue.py` |
| Entity resolution | Done | Splink pipeline (`src/tracegraph/er/`), `SAME_AS` / `POSSIBLE_SAME_AS` edges, held-out F1 0.642 ([docs/RESULTS.md](docs/RESULTS.md)) |
| Sanctions/PEP screening | Partial | Sanctions and offshore screening done (`GET /screen`, `ui/views/screening.py`); **no PEP data**: the OpenSanctions subset is OFAC SDN, EU FSF and UN SC only |
| Graph investigation | Done | Cited Q&A over six templates and guarded text-to-Cypher (`src/tracegraph/retrieval/`), subject profile (`GET /subjects/{id}`) |
| SAR draft | Done | `POST /sar`, grounded and labeled (`src/tracegraph/generation/sar_drafter.py`) |
| Disposition (close / escalate / file SAR) | Missing | No case or disposition concept anywhere |

### 2. Typology coverage: Partial

| Typology | Score | Notes |
| --- | --- | --- |
| Fan-in / fan-out | Done | `fan()` in `patterns.py`; held-out recall 15/22 and 11/27 |
| Round-tripping | Done (as cycles) | `cycles()`; held-out recall 23/24 |
| Layering | Partial | Path queries (`MONEY_PATH` template) and scatter-gather; no layering detector. IBM's **STACK** patterns (43) are loaded but not scored |
| Money mule networks | Partial | Scatter-gather covers one shape; IBM's **GATHER-SCATTER** (51) and **BIPARTITE** (49) patterns are loaded but not scored |
| Structuring / smurfing | Missing | IBM labels no structuring; needs injected ground truth from our own generator |
| Rapid movement of funds | Missing | No pass-through (in-then-out within hours) detector |
| Detectable as a Cypher pattern | Partial | Matching runs in Polars over edges read from Neo4j; an alert's evidence can be re-read with Cypher (`SAR_EVIDENCE` in `src/tracegraph/graph/cypher_templates.py`) |
| Explanation of why it was flagged | Done | Each alert stores a code-written summary and its thresholds ([ADR 010](docs/decisions/010-typology-detection.md)) |

### 3. Data: Partial

- **Done:** real OpenSanctions (3 sanctions lists) and ICIJ Offshore Leaks mixed with synthetic IBM
  transactions and synthetic customers (`src/tracegraph/ingest/`); provenance labeled in the README,
  API notices (`SYNTHETIC_NOTICE`, `ICIJ_NOTICE`, `ALERT_NOTICE` in `src/tracegraph/api/app.py`) and
  the UI; `synthetic_link: true` on every ER edge; licenses in `docs/DATA_LICENSES.md`.
- **Missing:** PEP data; FATF typology and FinCEN SAR guidance as a RAG corpus (Qdrant holds names
  only, so there is no document retrieval today); SAML-D; a per-field `provenance` value on API
  records (notices are per response, not per record).

### 4. Service layer: Partial

- **Done:** FastAPI with `/health`, `/ask`, `/sar`, `/alerts`, `/alerts/summary`, `/alerts/queue`,
  `/alerts/{id}`, `/subjects/{id}`, `/screen` (`src/tracegraph/api/app.py`); validated inputs; a
  typed client (`src/tracegraph/api/client.py`). No notebook-only logic.
- **Missing:** `/cases`, `/cases/{id}`, `/cases/{id}/sar-draft`, `/cases/{id}/disposition`.

### 5. Reproducibility: Partial

- **Done:** `.env.example`; pinned dependencies (`uv.lock`); seeded, deterministic staging tested
  under two hash seeds (`tests/test_staging_determinism.py`); idempotent loads that fail loudly on a
  stale graph; raw-data manifests with checksums (`data/raw/*/MANIFEST.tsv`).
- **Partial:** loading from scratch is a sequence of `make` targets (`ingest`, `er`, `er-write`,
  `qa-index`, `alerts`), not one seed command.
- **Missing:** `docker compose up` starts only Neo4j and Qdrant; the API and UI run natively
  (decision needed). Data download is manual by design: Kaggle needs an account.

### 6. Tests and CI: Partial

- **Done:** 216 unit tests, including detectors (`tests/test_detection.py`) and SAR/alert Q&A
  (`tests/test_sar_and_alerts_qa.py`); 22 integration tests against a throwaway Neo4j/Qdrant
  (`tests/integration/`); GitHub Actions running lint, mypy and all tests with service containers
  (`.github/workflows/ci.yml`), passing on recent pushes.
- **Partial:** no single integration test walks one case from alert to disposition (there is no
  case yet); the alert and SAR evidence path is covered (`tests/integration/test_alerts.py`).
- **Missing:** CI badge in the README.

### 7. Evaluation harness: Partial

| Requirement | Score | Where |
| --- | --- | --- |
| Typology precision/recall vs ground truth | Done | `eval/detection_eval.py`, dev-tuned, test scored once, degree baseline, bootstrap CIs |
| SAR quality via RAGAS | Missing (by decision) | ADR 009 dropped RAGAS; `eval/sar_check.py` measures grounding, labeling and abstention |
| Citation coverage | Partial | Every sentence must cite and pass the grounding check, but no "% of claims cited" figure is reported |
| Latency p50/p95 and hardware | Partial | QA reports mean and p95; SAR reports mean only; no p50; hardware (M2, 24 GB) is in the README text, not in result files |
| Failure analysis | Done | [docs/RESULTS.md](docs/RESULTS.md): homonym false positives, list-question failures, misread number, fan-out overfit |
| Entity resolution and leakage | Done (beyond the brief) | `eval/er_eval.py`, `eval/leak_test.py` |

### 8. Explainability and audit trail: Partial

- **Done:** every answer and SAR returns its evidence rows, cited IDs and the route; alerts store
  thresholds and a summary; ER edges store probability and method.
- **Missing:** subgraph visualization; any log of analyst actions (who, when, what, why).

### 9. Guardrails: Partial

- **Done:** input validation (Pydantic `Field` limits and ID patterns in `api/app.py`, client-side
  checks in `ui/`); read-only, schema-checked, limited Cypher; "insufficient evidence" after one
  failed regeneration; clarifying question for ambiguous names; `DRAFT – requires analyst review`.
- **Partial:** human review is a label only; nothing enforces review before a disposition, because
  there is no disposition.
- **Missing:** PII masking. Uvicorn's access log prints full request paths, so `/screen?name=...`
  and `/subjects/{id}` write names and IDs to the console.

### 10. Observability: Partial (weak)

- **Done:** one usage line per LLM request (calls, tokens, LLM time) via `MeteredProvider`
  (`src/tracegraph/llm/metering.py`); `/ask` returns a trace of its steps.
- **Missing:** structured (JSON) logs, request IDs, tracing (Langfuse or otherwise).

### 11. Data quality: Partial

- **Done:** Pandera schemas on every staging table (`src/tracegraph/ingest/staging_schemas.py`);
  Pydantic settings and API models; name variants (typos, transliteration, reordering, initials,
  aliases, suffix removal) and data-entry noise for every customer role ([ADR 006](docs/decisions/006-seed-independent-customers-and-icij-addresses.md));
  missing source birth dates handled.
- **Missing:** duplicate customer records, customers with missing required fields, and late-arriving
  transactions, each injected and handled with a test.

### 12. Simulated live feed: Missing

No streaming generator or scheduler; alerts are written once by `make alerts`.

### 13. Analyst UI: Partial

- **Done:** alert queue with filters and paging, alert review panel, subject profile, screening,
  assistant, SAR page (`ui/`).
- **Missing:** graph visualization in the case view; an editable SAR draft (it is shown read-only
  with a download); disposition buttons.

### 14. Demo: Missing

The provider is configurable through `.env` (`src/tracegraph/llm/factory.py`), so a small model or
a free-tier OpenAI-compatible provider works today, but there is no documented demo mode, no hosted
instance and no GIF.

### 15. README as a design doc: Partial

Has the user, workflow, why Graph-RAG, trade-offs, results with sources and limitations. Missing:
a Mermaid diagram, demo GIF slot, quickstart via `docker compose up`, and the "what it would take to
go to production" section. The problem framing (alert volume, false-positive burden) needs a cited
public source for any figure, or stays qualitative.

## Gaps ranked by impact on a hiring manager ÷ effort

Impact and effort are judged 1–5 (5 = high); higher ratio first.

| Rank | Gap | Impact | Effort | Ratio |
| --- | --- | --- | --- | --- |
| 1 | Citation coverage and latency p50/p95 + hardware, computed from existing result files | 3 | 1 | 3.0 |
| 2 | PII masking and structured JSON logs with request IDs | 3 | 1 | 3.0 |
| 3 | Cases, disposition with mandatory rationale, append-only analyst audit log (API) | 5 | 2 | 2.5 |
| 4 | UI case view: subgraph picture, editable SAR, disposition buttons | 5 | 2 | 2.5 |
| 5 | New typologies scored on IBM labels: layering (STACK), mule networks (GATHER-SCATTER, BIPARTITE), rapid movement | 4 | 2 | 2.0 |
| 6 | README design doc with Mermaid and production section (last, per the brief) | 4 | 2 | 2.0 |
| 7 | CI badge in README | 2 | 1 | 2.0 |
| 8 | Demo-mode docs and GIF placeholder | 2 | 1 | 2.0 |
| 9 | Full-case integration test (alert → case → SAR → disposition) | 3 | 2 | 1.5 |
| 10 | Cypher pattern per typology for explanation (alert subgraph query) | 3 | 2 | 1.5 |
| 11 | Data-quality injections (duplicates, missing fields, late transactions) | 3 | 2 | 1.5 |
| 12 | One seed command, and API + UI in Compose (Ollama stays native) | 4 | 3 | 1.3 |
| 13 | FATF/FinCEN guidance corpus in Qdrant, cited in SAR drafts | 4 | 3 | 1.3 |
| 14 | Structuring/smurfing via an injected generator | 3 | 3 | 1.0 |
| 15 | Simulated live feed raising alerts on a schedule | 3 | 3 | 1.0 |
| 16 | PEP screening data | 2 | 2 | 1.0 |
| 17 | SAR quality via RAGAS | 2 | 3 | 0.7 |
| 18 | Self-hosted Langfuse tracing | 2 | 4 | 0.5 |
| 19 | SAML-D as a second transaction source | 2 | 4 | 0.5 |

Ties are ordered by importance to the investigator workflow.

## Phase plan

Each phase ends with tests, `make smoke`, a short summary and one commit per logical change. Work
would go on a new branch from `main` after `phase2-er` is merged.

| Phase | Contents (gap ranks) | Notes |
| --- | --- | --- |
| 1. Case workflow | Cases, disposition, audit log, `/cases*` endpoints (3); UI case view with subgraph, editable SAR, disposition buttons (4); full-case integration test (9) | Disposition requires an analyst name and rationale; "file SAR" requires a reviewed draft. The copilot never closes or files on its own |
| 2. Guardrails and observability | PII masking, JSON logs, request IDs (2); Langfuse as an optional Compose profile, if you choose it (18) | Memory: Langfuse v3 adds Postgres, ClickHouse, Redis and MinIO to a laptop that already runs Neo4j |
| 3. Reproducibility | One seed command; API and UI containers in Compose, calling native Ollama (12); CI badge (7) | Needs your decision on ADR 009 |
| 4. Typologies | Layering, mule networks, rapid movement scored on IBM labels (5); structuring via injected generator (14); Cypher explanation per typology (10) | New detectors tuned on the existing dev split and scored on test once; new held-out numbers reported as they come |
| 5. Regulatory RAG corpus | FATF and FinCEN guidance chunks in Qdrant; SAR drafts may cite guidance passages alongside graph IDs (13); PEP data if approved (16) | Licenses must be checked first (FinCEN documents are US government works; FATF terms need review) |
| 6. Data quality and live feed | Duplicate customers, missing fields, late transactions with tests (11); streaming generator raising alerts on a schedule (15) | |
| 7. Evaluation | Citation coverage, p50/p95, hardware in result files (1); a fresh SAR check run for the new case flow; RAGAS only if chosen (17) | Old held-out files are kept; new runs are reported as new runs |
| 8. Demo and README | Demo-mode docs and GIF placeholder (8); README rewrite to the brief's structure (6) | The hosted demo needs hosting and credentials from you |

## Decisions needed

1. **RAGAS:** keep ADR 009 (gold answers, grounding and citation coverage), or add RAGAS
   faithfulness and context precision with a disclosed local judge model?
2. **`docker compose up`:** add API and UI containers that call Ollama on the host (Ollama stays
   native for the GPU), superseding part of ADR 009?
3. **LangGraph:** keep the fixed pipeline and draw it as such in the Mermaid diagram, or wrap it in
   LangGraph?
4. **Detectors in Cypher:** keep Polars matching and add a Cypher explanation query per alert, or
   rewrite the detectors in Cypher?
5. **Langfuse:** include it as an optional Compose profile, or stop at structured logs?
6. **New data:** FATF/FinCEN corpus, an OpenSanctions PEP subset, SAML-D: which ones, after a
   license check?
7. **Where cases live:** `Case` and `Disposition` nodes in Neo4j (one store, graph-linked), or a
   separate SQLite audit store? Recommended: Neo4j nodes written only by fixed endpoints, with an
   append-only `AuditEvent` per action.
8. **Analyst identity:** there is no authentication; record a self-declared analyst name, disclosed
   as a prototype limit?

## Approved decisions (2026-11-18)

The user approved the phase plan with the recommended defaults:

1. **RAGAS:** not added; ADR 009 stands. SAR quality is measured by grounding, labeling,
   abstention and a new citation-coverage figure.
2. **Compose:** API and UI get containers that call Ollama on the host; Ollama stays native.
3. **LangGraph:** not added; the fixed pipeline is drawn as a pipeline.
4. **Detectors:** matching stays in Polars; each typology gets a Cypher query that re-reads an
   alert's subgraph as its explanation.
5. **Langfuse:** not added; structured JSON logs with request IDs instead.
6. **New data:** FATF/FinCEN guidance corpus and an OpenSanctions PEP subset, each after a license
   check; no SAML-D.
7. **Cases:** `Case` and `AuditEvent` nodes in Neo4j, written only by fixed endpoints; audit
   events are append-only.
8. **Analyst identity:** a self-declared analyst name, disclosed as a prototype limit.
