# ADR 011: Investigator UI and evaluation report (Phase 5)

- **Status:** Accepted (2026-11-15)

## Context
Phase 5 adds a UI for analysts and a `make eval` command. Two constraints shaped both:
- The guards (read-only Cypher, the grounding check with regeneration and abstention, the DRAFT
  label, the fixed notices) live in the API's answer and SAR paths. A UI with its own database or
  LLM access could bypass them.
- The held-out evaluations (leak test, QA check, detection test, SAR check) are run once each
  (CLAUDE.md). A `make eval` that re-ran them would produce new held-out runs every time, and
  re-running QA alone takes about 15 minutes on the local model.

## Decisions
1. **The Streamlit UI (`ui/app.py`, `make ui`) is a thin client of the API.** It calls `/health`,
   `/ask`, `/alerts` and `/sar` through `tracegraph.api.client` (httpx) and never opens a
   database session or calls the LLM. `UI_API_URL` and `UI_REQUEST_TIMEOUT_SECONDS` live in
   `config.py`. The timeout is 300 s because SAR drafts take about 90 s, and longer when one is
   regenerated.
2. **`GET /alerts`** lists alerts by `subject` (account or customer ID, reusing the Q&A alerts
   template) or by `typology` (a new `ALERTS_BY_TYPOLOGY` template, ordered by ID). Exactly one
   filter is allowed, the typology is an enum, the limit is capped at 100 and at `qa_max_rows`,
   and the query runs through the same read-only, time-limited `run_read`. There is no LLM step,
   so browsing alerts is instant and deterministic. Results carry the fixed alert notice.
3. **`make eval` (`eval/report.py`) collects and re-runs nothing.** It reads every
   `eval/results/*.json` file the README links to, fails if one is missing, and writes
   `eval/results/eval_<timestamp>.json` and `.md` with the headline numbers and the LLM provider
   and model recorded in each source file. The README therefore cannot cite a result file that
   does not exist without `make eval` failing.

## Evidence
- **Live check (2026-11-15, native API, Docker Neo4j and Qdrant, Ollama `qwen2.5:7b-instruct`):**
  - `GET /alerts` for account 19329:810E5B020 returned its two alerts (a cycle and a fan-in);
    `typology=CYCLE` returned cycle alerts in ID order.
  - In the browser, the Alerts tab listed the same two alerts with the notice. "Use this alert"
    carried `alert-90f996d7938c9778` to the SAR tab, which returned a grounded draft beginning
    `DRAFT – requires analyst review`.
  - The Ask tab answered the README example with one cited sentence and 7 evidence items.
- **Tests:**
  - API: both filters, the exactly-one rule, an injected typology string and an oversized limit
    rejected with 422 before any query.
  - Client: endpoints and payloads over a mock transport, and HTTP errors, refused connections
    and timeouts shown as readable errors.
  - Page (Streamlit `AppTest`): it renders three tabs without calling the API, rejects a
    malformed alert ID in the page, and shows a drafted SAR with its label.
  - Report: cited-file parsing, LLM recording, and failure on a missing file.
  - Integration: `ALERTS_BY_TYPOLOGY` against the fixture graph.
- **Report:** `make eval` on the current README collects 5 files (QA seeds 44 and 43, detection
  test, SAR check, leak test).
