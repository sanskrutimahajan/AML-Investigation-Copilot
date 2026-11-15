# Worklog

Dated entries for issues in progress: findings, attempts and interim numbers. When an issue is
resolved (fix implemented, tests pass, verification re-run), its final decision, reason and evidence
are summarized in the relevant ADR, and the entry is marked **CLOSED**. See CLAUDE.md "Working style".

---

## 2026-10-19 — Phase 2 label noise: accidental name collisions — OPEN (pending final verification)
- **Finding:** preliminary Step A dev results showed many "false positives" that were exact matches,
  for example plain customer "wu yi chao" = source "wu yi chao", and "formia ltd" = "formia limited".
  216 plain customers (0.6%) were ER-equivalent to a real record but labeled non-match. The
  freshness check compared raw text against the seed pools only.
- **Fix:** plain and look-alike names are checked against the (kind, `name_signature`) of *every*
  source record and alias, on the final name after edits. Look-alike names get their own random
  stream. Collisions are now 0 among plain customers and look-alikes.
- **Remaining:** final verification on the corrected stage (leak test, Step A on dev).

## 2026-10-19 — Homonym stratum and attribute independence — **CLOSED** 2026-11-15 → [ADR 007](decisions/007-entity-resolution.md)
- **Added:** 375 homonyms (`homonym_rate` 0.01). Each takes an exact source name after
  normalization and is labeled non-match, with a conflicting DOB or country.
- **Attempt 1:** conflicting country drawn only from the 8 base countries. Leak AUC seeds vs
  homonyms was **0.795**; the drivers were country (categorical group 0.79) and address (0.70).
- **Attempt 2:** country drawn from the shared rule, plus signature-preserving edits. Not tested on
  its own; superseded by the refactor below.
- **Refactor (user spec):** one `shared_attributes()` function now produces every non-name
  attribute for every role. A role decides only the name source and the homonym conflict, which is
  chosen at random among DOB and country, each drawn from the shared rule excluding the source's
  value. `tests/test_attribute_independence.py` runs chi-square/KS checks on 8 attributes × 3 roles.
  The leak test now has a 4-way breakdown.
- **Leak test after refactor** (seed 42, stage before the ICIJ determinism fix):

  | Comparison | Gradient boosting AUC |
  | --- | --- |
  | Plain vs seeds | 0.579 |
  | Plain vs look-alikes | 0.515 |
  | **Plain vs homonyms** | **0.601 ± 0.025** |
  | Seeds vs homonyms | 0.565 |
  | Seeds vs look-alikes | 0.587 |
  | Seeds + look-alikes vs plain | 0.541 |

  The drivers of 0.601 are:
  - **Structural:** homonyms never get initials. The initial footprint "`. `" appears in 2.1% of
    homonyms vs about 10.5% of everyone else.
  - **Selection:** the eligibility rule (source has a DOB or country) over-represents "Mr" (1.6% vs
    0.4% of seeds) and "Pte Ltd" (0.8% vs 0.0%).
- **User decision (2026-10-20):** accepted as an explicit **judgment call, not a pass**. It is
  within the ±0.025 CI, it comes from the spec constraint that homonyms are exact matches, and
  Splink has no standalone name-style feature.
  - 0.601 becomes the homonym leakage baseline.
  - Phase 2 must report the homonym false-positive rate and show it is not explained by name style
    (`homonym_name_style_check` in `er_eval.py`).
  - Step B risk: the LLM sees raw names, so check its homonym decisions for reliance on honorifics
    and initials.
- **Disclosures, country share outside the 8 base countries** (seed 42 is official and never
  changed):

  | Role | Seed 42 | Mean of seeds 1–5 |
  | --- | --- | --- |
  | Plain | 0.453 | 0.452 |
  | Seeds | 0.505 | 0.463 |
  | Look-alikes | 0.427 | 0.466 |
  | Homonyms | 0.416 | 0.430 |

  The seed gap is chance: chi-square p = 0.0007 at seed 42, but only +1.1 points across seeds 1–5.
  The homonym gap of −2.2 points is by design: a conflicting country excludes the source's own.
- **Remaining:** re-run the leak test on the corrected stage. "Close" means every AUC within ±0.025
  of the accepted run, and none above 0.625; otherwise stop and report. Then reset, ingest and run
  Step A on dev.

## 2026-10-20 — ICIJ sampling nondeterminism — **CLOSED** 2026-11-15 → [ADR 007](decisions/007-entity-resolution.md)
- **Finding:** `REGISTERED_ADDRESS` was 2,498 in one load and 2,492 in the next, with no code change.
  Three identical runs of `build_icij_stage` gave node sets differing by 2 nodes.
- **Cause:** `officer_addresses.unique().filter(~officer_addresses.unique().is_in(...))`. Polars
  `unique()` is unordered, so the mask from the second call was applied to the first call's order.
- **Fix:** compute `unique()` once and sort. Three runs are now identical (35,761 ICIJ relationships),
  and IBM sampling was verified deterministic. Other `unique()` uses were audited; they are
  set-membership only or sorted before use.
- **CI determinism test added** (`tests/test_staging_determinism.py`): stages the fixture twice and
  asserts identical SHA-256 checksums for every Parquet file (staging, ground truth, seeds, edit log),
  plus an identical manifest apart from the timestamp and data path.
  - The first version **did not catch the bug**: it passed 10/10 with the bug reintroduced. The bug
    only bites when some officer addresses are already 1-hop neighbours, because only then does the
    misaligned mask contain `False`. Polars `unique()` order is unstable even at n = 10, so fixture
    size was not the issue.
  - Added addresses shared between seed companies and officers (the registered-agent case). Now it
    fails 10/10 with the bug and passes 10/10 with the fix.
- **Remaining:** the corrected-stage verification above.

## 2026-10-19 — Step A baseline (dev) — OPEN
- **Preliminary dev numbers on the noisy-label stage, superseded:** full-model F1 0.561, blocking
  pair completeness 0.982, customer-level AUC 0.913 vs leak 0.578, and a review queue of 27,168 pairs
  because the band fell to the score floor.
- **Model changes since:** best-over-variants token overlap; soft-token comparison for typos,
  reorders and initials; log-odds in the combined test; embedding cache; review band set by a queue
  cap of 1,000 pairs per split; recall gain reported at queue sizes 100/250/500/1,000.
- **Remaining:** run on the corrected stage, report dev results, and stop.

## 2026-10-20 — Leak test on the corrected stage (after the ICIJ determinism fix) — **CLOSED** 2026-11-15 → [ADR 007](decisions/007-entity-resolution.md)
- **"Close" criterion (user):** every AUC within ±0.025 of the accepted run, and none above 0.625.

  | Comparison | Accepted (LR / GB) | Corrected stage (LR / GB) | Verdict |
  | --- | --- | --- | --- |
  | Plain vs seeds | 0.512 / 0.579 | 0.523 / 0.580 | ok |
  | Plain vs look-alikes | 0.516 / 0.515 | 0.504 / 0.473 | **GB −0.042** |
  | Plain vs homonyms | 0.545 / 0.601 | 0.586 / 0.611 | **LR +0.041** |
  | Seeds vs homonyms | 0.562 / 0.565 | 0.559 / 0.550 | ok |
  | Seeds vs look-alikes | 0.529 / 0.587 | 0.500 / 0.546 | **LR −0.029, GB −0.041** |
  | Seeds + look-alikes vs plain | 0.520 / 0.541 | 0.523 / 0.550 | ok |

- No AUC is above 0.625 (max 0.611), but four values moved more than ±0.025, three of them downward.
  The CV standard deviations are 0.013–0.039, so ±0.025 is about as tight as run-to-run noise.
- **Result file:** `eval/results/` (seed 42, corrected stage). The pipeline is stopped before reset,
  ingest and Step A, pending the user's decision.

## 2026-10-20 — LLM provider: local Ollama by default, any provider through config — **CLOSED** 2026-11-14 → [ADR 008](decisions/008-llm-provider.md)
- **User direction:** no paid API. Every LLM step uses a local model via Ollama (native macOS). The
  machine is an M2 with 24 GB, so the model is `qwen2.5:7b-instruct`, being downloaded. Any provider
  must work through `.env` alone: `LLM_PROVIDER=ollama|openai_compatible|anthropic`, `LLM_BASE_URL`,
  `LLM_API_KEY`, `LLM_MODEL`, plus `JUDGE_*` settings that default to the main ones.
- **Implemented:**
  - `llm/factory.py` (provider from config, per role), `llm/structured.py` (JSON schema, Pydantic
    validation, one retry, strict schemas).
  - `OpenAICompatProvider`, shared by Ollama and every compatible endpoint; it falls back to
    `json_object` mode if `json_schema` is rejected.
  - `AnthropicProvider`, which sends no `temperature` (current Claude models reject sampling
    parameters) and requests JSON through `output_config.format`.
  - The old `LLM_ENABLED` / `ANTHROPIC_*` / `OPENAI_COMPAT_*` settings are removed; setting
    `LLM_API_KEY` for a remote provider is the opt-in.
  - Manifests now exclude every `*password*` / `*api_key*` setting. The rename would otherwise have
    leaked `llm_api_key` into `stage_manifest.json`.
  - Docs: the "LLM providers" section in CLAUDE.md, and "LLM" plus "Using a different LLM" in README.
- **Tests:** 11 mocked provider tests: selection for each provider, judge fallback, schema sent,
  no temperature for Anthropic, JSON retry and give-up. No network.
- **Plan change:** Step B now uses the local model, not the Anthropic API from the approved plan.
  There is no cost estimate or spend cap, because nothing is billed.
- **Closed (2026-11-14):** the live smoke test passed (40/40 valid JSON, no retries; see the Step B
  entry). Summarized in ADR 008.

## 2026-11-14 — Leak drift: noise or a real change? — **CLOSED** 2026-11-15 → [ADR 007](decisions/007-entity-resolution.md)
- **Question:** are the four ±0.025 moves in the corrected-stage leak test (2026-10-20 entry) noise?
- **Method:** the three moves' comparisons re-run on the current stage over 10 evaluation draws,
  varying the CV fold split, the plain-negative subsample and the model `random_state`. Same
  features and models as `eval/leak_test.py`. A one-off diagnostic, not added to the repo.

  | Comparison | Current stage, 10 draws (mean ± SD, range) | Corrected run (z) | Accepted run (z) |
  | --- | --- | --- | --- |
  | Plain vs look-alikes, LR | 0.490 ± 0.011 (0.475–0.502) | 0.504 (+1.3) | 0.516 (+2.5) |
  | Plain vs look-alikes, GB | 0.498 ± 0.012 (0.481–0.518) | 0.473 (−2.0) | 0.515 (+1.4) |
  | Plain vs homonyms, LR | 0.572 ± 0.009 (0.557–0.586) | 0.586 (+1.6) | 0.545 (−3.0) |
  | Plain vs homonyms, GB | 0.604 ± 0.009 (0.591–0.619) | 0.611 (+0.8) | 0.601 (−0.4) |
  | Seeds vs look-alikes, LR | 0.493 ± 0.013 (0.478–0.526) | 0.500 (+0.5) | 0.529 (+2.7) |
  | Seeds vs look-alikes, GB | 0.534 ± 0.012 (0.514–0.553) | 0.546 (+1.0) | 0.587 (+4.4) |

- **Finding:** the corrected run is a typical draw on its own data. The accepted run's values lie
  outside that spread, by more than evaluation noise in both runs together can explain. So the moves
  are a **real data change**: the ICIJ determinism fix changed which ICIJ records are sampled and
  seeded. The change mostly lowers leakage. The one rise is plain vs homonyms LR (+0.027). The
  strongest classifier (plain vs homonyms GB) is unchanged at 0.604, and no draw exceeds 0.619 (< 0.625).
- **User decision (2026-11-15):** the corrected stage is accepted as the leakage baseline. The
  homonym baseline is **0.604**, the 10-draw mean for plain vs homonyms with GB, replacing the
  single-draw 0.601. The graph is reset and re-ingested. Goes into ADR 007 when Phase 2 closes.
- **Also found:** `data/processed/er/` holds an unlogged, never-evaluated `make er` run on the
  corrected stage (2026-10-20 13:37Z, 2.80M candidate pairs, both variants). Nothing in Neo4j
  depends on it.

## 2026-11-14 — Step B: LLM adjudication of the review band — OPEN (code done; not yet run or evaluated)
- **Implemented:** `er/adjudicator.py` (`make er-adjudicate`).
  - The configured LLM decides each review-band pair: `match`, `non_match` or `unsure`.
  - Only `match` becomes `SAME_AS`, with method `<model>+llm` and the reason in `evidence`.
    `non_match` gets no edge. `unsure` and invalid JSON (`failed`) stay `POSSIBLE_SAME_AS`.
  - The LLM sees raw names, which the user chose over normalized ones, and never the model score.
  - Decisions are saved after every pair, so a run resumes. A rerun refuses to mix thresholds,
    model or prompt version.
- **Writer and eval:**
  - `writer.py` and `er_eval.py` share `final_edges`, so the eval scores exactly what is written.
  - `er_eval.py` adds an `llm_adjudication` section: decisions by truth, F1 with and without the
    LLM, a paired customer-cluster bootstrap CI for the F1 gain, homonym and look-alike FP rates,
    and `homonym_name_style_check` on the LLM's final decisions.
  - `er-test` refuses to run until every review-band pair has a decision, because the test split
    is scored only once.
- **Prompt facts checked against the generator:**
  - Seeds copy the source DOB whenever the source has one, so a DOB conflict is real evidence.
  - Seeds copy the source country only about 50% of the time, so the prompt calls country weak
    evidence and forbids non_match on country alone.
  - Seed edits never drop or replace a whole name word (typo, transliteration, reorder, initial,
    suffix removal), so "a surname that is entirely different means different people" cannot
    reject a true seed match.
- **Smoke test** (20 random dev band pairs at the untuned 0.92/0.75 thresholds; labels not looked at):
  - Prompt v1: 19 of 20 `unsure`, with field lists instead of reasons. It said when names may
    differ, but never what makes two parties different.
  - Prompt v2 adds the different-surname / distinctive-word rule, narrows `unsure` and asks for a
    name comparison. Result: 5 match, 14 non_match, 1 unsure.
  - By eye, some matches look wrong (a different surname; the generic word "HOLDING"), and some
    reasons call a DOB "different" when one side is unknown.
  - No further prompt changes by eye. Decision quality is judged only by the dev eval.
- **Numbers:**
  - Latency was 2.1 s per pair for v1 and 6.8 s for v2; a full run of about 2,000 band pairs would
    take about 3.8 h.
  - The dev band at the untuned thresholds is 3,178 pairs. Re-tuning on the corrected stage
    should bring it back to the 1,000-pair cap.
- **Tests:** 5 unit tests (band, prompt, resume / refusal, final edges, threshold check), the
  paired-CI metric test, and 2 writer integration tests. Totals: 134 unit, 11 integration, all passing.
- **Remaining:** depends on the leak decision. Then: graph reset and ingest → `make er` → `make er-tune` →
  set thresholds → `make er-adjudicate` → `make er-tune` (dev LLM metrics) → `make er-test` once →
  `make er-write` → ADR 007.

## 2026-11-15 — Customer address nondeterminism — **CLOSED** 2026-11-15 → [ADR 006](decisions/006-seed-independent-customers-and-icij-addresses.md)
- **Finding:** after the graph reset, `make ingest` re-staged, and `customers.parquet` no longer
  matched its earlier checksum; every other staging and ground-truth file did. Staging again into a
  temporary directory with the same code gave yet another `customers.parquet`, so this is
  nondeterminism, not a code change.
  - Only the **city part of `address`** differs: 2,617 of 37,515 customers. The examples seen are
    Italian addresses.
  - Names, DOB, country, IDs, ground truth, seeds and the edit log are identical across runs.
- **Likely cause:** an unordered city list for some countries (as in the ICIJ `unique()` bug). Not
  yet confirmed.
- **Impact:**
  - ER: none; customer addresses are excluded from ER features by design.
  - Leak test: uses address n-grams, so it needs a re-run after the fix.
  - The fixture-based determinism test misses this.
- **Evidence lost:** `git stash` / `stash pop` on 2026-11-14 (a mypy comparison) reset source
  modification times, so they cannot show which code produced the 10-20 stage.
- **Graph state:** reset and re-ingested on 2026-11-15 from the current stage (load verified,
  139,141 nodes, 420,759 relationships). There are no ER edges yet. `make er` and `make er-tune`
  have not run.
- **To do on return:** fix the cause, extend the determinism test so it fails on this bug, re-stage,
  re-run the leak test (check against the accepted baseline), re-ingest, then continue ER.

## 2026-11-15 — Phase 3: retrieval and cited Q&A — **CLOSED** 2026-11-15 → [ADR 009](decisions/009-retrieval-and-qa.md)
- **Plan approved (user, 2026-11-15):**
  - Qdrant is used only for name linking, because the data has no free text. This becomes ADR 009.
  - A 50-question QA check with gold answers from staging.
  - Watchlist questions on real data wait for Phase 2 edges.
- **Built:**
  - Five Cypher templates.
  - A read-only retriever with a row cap and timeout.
  - Guarded text-to-Cypher: a static check, then `EXPLAIN` (read-only type, exact columns, schema).
  - Linking: ID patterns, plus Lucene fuzzy search and Qdrant names fused with RRF.
  - The router, LLM drafting, the grounding check, and `POST /ask`.
  - `make qa-index` and `make qa-check`.
- **Verified on the real graph:**
  - Neo4j rejects writes in read sessions (`Neo.ClientError.Statement.AccessMode`).
  - Every template runs in under 0.5 s once lookups use indexed `CALL () {...UNION...}` subqueries;
    `OR EXISTS {...}` scanned all 53,600 accounts.
  - `EXPLAIN` catches unknown labels, relationship types and properties, wrong columns and syntax
    errors.
- **Found in live runs, fixed:**
  - `CachedEmbedder` rewrote the 277 MB cache on every miss (5–15 s per name), so Q&A uses the
    plain embedder.
  - A fixed vector-score floor cannot separate nonsense (0.83) from real names (0.93). A
    vector-only candidate now needs a score of at least 0.9; full-text candidates are always kept.
  - IBM reuses bank names (3,975 names across 5,631 banks), so a bank name alone is ambiguous by
    data.
  - The model wrapped citations in brackets, and digits inside IDs were read as numbers. Both are
    cleaned before grounding, and numbers in the question are allowed.
  - The router invented `opensanctions:Q8000488C` for a name. Mentions must now appear in the
    question; there is one re-ask, then invented mentions are dropped.
  - Text-to-Cypher wrote `country: 'Italy'`. The schema prompt now gives value formats (ISO codes,
    currency names).
  - The 7B model misread a 4-payment path as "not connected". Path answers are now written by code
    and still pass the grounding check.
  - `shortestPath` ignores time; the first real path had payments out of order. Answers claim a
    flow of funds only when the payments are in time order, otherwise "a connection, not a traced
    flow".
- **Tests:** 172 unit tests (text-to-Cypher attack strings, grounding, routing, fusion, path
  answers, the draft loop, the API) and 18 integration tests (templates against Parquet-computed
  totals, read-only enforcement, `EXPLAIN` checks, linking with in-memory Qdrant).
- **QA check, dev run (seed 42, 2026-11-15 04:38Z,** `eval/results/qa_check_20261115T043809Z`):

  | Measure | Rate (95% CI) |
  | --- | --- |
  | Answer accuracy | 0.52 (0.39–0.65) |
  | Intent accuracy | 0.80 |
  | Link accuracy | 0.78 |

  - **By family:** networks 10/10, totals 6/10, holdings 5/10, paths 5/10, lists 0/10.
  - **All 24 failures were refusals, clarifying questions or "not found";** no wrong number reached
    an answer. There were three causes:
    - (A) the router kept "account" in a mention, so the ID did not fullmatch (9 failures);
    - (B) the "no path" sentence says "up to 4 payments", but 4 was not in the cited evidence, so
      my own grounding self-check rejected it (5 failures; my unit test checked the text, not the
      grounding);
    - (C) list questions were routed to a template, with a description used as a name (10 failures).
  - Every question that linked correctly was answered correctly.
  - **Fixes:** IDs are found inside longer mentions; the negative path's evidence records
    `payments_searched_up_to`; the router prompt says a description is not a mention. Regression
    tests were added for A and B.
  - **Held-out discipline:** seed 42 is now the development set, because the router prompt was
    changed after looking at it. The reported result is a fresh seed-43 set run once after the
    fixes (`--seed`).
- **Address fix verified (2026-11-15):**
  - Cause: Faker `it_IT` builds `cities` with `list({...})`, so its order follows `PYTHONHASHSEED`.
    `seeded_faker` sorts list-valued `cities`; only `it_IT` has one.
  - `tests/test_faker_determinism.py` (all locales, two hash seeds) and the staging determinism
    test (now two subprocesses with different hash seeds) both fail with the bug reintroduced and
    pass with the fix. The old staging test ran both stagings in one process, so it could not see
    hash-order effects.
  - Re-staging changed only `customers.parquet`.
- **Leak test, one run after the fix** (`leak_test_20261115T060250Z`): the maximum is 0.621
  (homonyms vs plain, GB). That is above the 0.60 target but within the accepted 0.625 ceiling.
  Every comparison involving seeds is at most 0.569. Reported as it came out, in the README.
- **Phase 3 held-out QA (seed 43, run once,** `qa_check_20261115T051015Z`):
  - Answer accuracy 0.80 (0.67–0.89). Link accuracy 1.00. Holdings, totals, paths and networks
    10/10 each; lists 0/10.
  - Two list questions were answered with grounded facts about the wrong customer. Disclosed in the
    README, not tuned.
- **User decisions (2026-11-15):**
  - The API runs natively, and `make smoke` runs before any commit that touches the API or the
    README examples.
  - RAGAS is dropped, along with its `JUDGE_*` settings.
  - Only the result files the README cites are committed.
  - Approvals are needed only for scope, data or evaluation methodology.
  - No tuning after held-out or leak-test numbers.
- **Smoke test passed** on the live stack (native API, Docker Neo4j and Qdrant, Ollama).
- **Graph state:** Neo4j still holds the pre-fix customer addresses. A reset and re-ingest is
  needed when Phase 2 resumes; it needs user approval because it deletes data.

## 2026-11-15 — Phase 4: typology detection, alerts, SAR drafts — **CLOSED** 2026-11-15 → [ADR 010](decisions/010-typology-detection.md)
- **User decisions:** the evaluation method (grouped seed split, dev tuning, a single test score,
  pattern recall plus precision against the base rate, an equal-volume degree baseline with a
  paired bootstrap); all four detectors; a minimal grounded SAR drafter; alerts in Q&A.
- **Profiling before design:** see ADR 010 (hub sizes, cycle lengths 2–12, patterns lasting 3–4
  days).
- **Found while building:**
  - Cycle alerts multiplied with repeated payments (3,959 alerts on 1,104 accounts). They are now
    de-duplicated by account set.
  - The loader's stale-graph check failed on any non-staging node, so `Alert` nodes would have
    broken every later ingest. Alerts are now excluded, like ER edges.
  - A first SAR draft read the threshold `min_senders=6` as "6 sender accounts" (the alert had
    15), and described scatter-gather payments as "between the two accounts". The grounding check
    cannot catch a number that is present but misread. Alerts now carry a code-written `summary`,
    and `parameters` is renamed `detection_thresholds`. The re-drafts were correct.
- **Results:** the dev choice and the single test score are in ADR 010 and the README. Fan-out does
  not clearly beat the degree baseline.
- **QA re-run:** the router gained an `alerts` intent, so the QA table needs a fresh held-out set
  (seed 44, run once, with an alerts family). Seed 43 stays as the recorded Phase 3 run.
- **Held-out QA, seed 44 (run once):** 47/60 (0.78, 0.66–0.87). Alerts 9/10, holdings 9/10,
  totals 9/10, paths 10/10, networks 10/10, lists 0/10. The three non-list misses were incomplete
  answers (a missing currency, a missing alert, an owner without their ID), not wrong facts.
- **SAR check:** 16 of 20 grounded (15 on the first try). 4 abstained, one of them on invalid JSON.
  All were labeled, none used accusatory words, and mean latency was 90 s.
