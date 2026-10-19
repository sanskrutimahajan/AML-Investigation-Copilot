# Phase 2 plan: Entity resolution (approved 2026-10-19)

## Goal and scope
Link synthetic `Customer` nodes to `SourceEntity` records (OpenSanctions + ICIJ) and write
`SAME_AS` edges with calibrated probabilities and provenance. This is **customer screening**:
for each customer, which watchlist or offshore records (if any) are the same party?

- **In scope:** Customer ↔ SourceEntity.
- **Out of scope:** SourceEntity ↔ SourceEntity across OpenSanctions and ICIJ. There is no ground
  truth for it, and ICIJ already ships its own `ICIJ_SAME_AS` / `ICIJ_SIMILAR` links. It can be
  added later as qualitative output only.
- **No ground truth in the model:** ground truth is used **only** to evaluate. Splink is trained
  without supervision, and thresholds are chosen on a development split, never on the test split.

## Files and functions
| File | Contents |
| --- | --- |
| `er/normalize.py` | `normalize_name` (casefold, `anyascii` folding, punctuation, whitespace); `split_legal_suffix` (returns core + suffix class); `canonical_tokens` (order-insensitive token set, initials kept as a separate feature); `phonetic_keys` (jellyfish Double Metaphone / NYSIIS per token); `normalize_country`, `birth_year` |
| `er/blocking.py` | Candidate generation as the union of: (a) **rare-token blocking**, meaning pairs that share a normalized token whose document frequency is below a cap, so common words like "trading" or "limited" don't block; (b) **phonetic surname key** for people; (c) **embedding nearest neighbours**, top-k from Qdrant over source names and aliases. Customer type must match the source schema (person ↔ Person/Officer, organization ↔ the rest). Reports pair completeness and reduction ratio |
| `er/embeddings.py` | Embeds source names and aliases with `BAAI/bge-small-en-v1.5` (MIT) into a Qdrant collection (Phase 2 is the first phase to use Qdrant); embeds customers at query time; cosine similarity used as a feature |
| `er/splink_model.py` | Splink (DuckDB backend), unsupervised: `u` estimated by random sampling, `m` by EM on blocked pairs. Comparisons: Jaro-Winkler levels on the normalized name; best match against the source's aliases (precomputed); token-set ratio (rapidfuzz, precomputed); embedding-cosine levels; same legal-suffix class; DOB (exact / same year / missing); country (exact / missing). Term-frequency adjustment on name tokens, so frequent tokens count for less |
| `er/adjudicator.py` | **Step B only.** Middle-band pairs (`er_review_threshold` ≤ p < `er_match_threshold`) go through the existing `LLMProvider` interface (Anthropic API). There is a hard spend cap in config, and pairs the LLM is unsure about stay in the review queue. In Step A, middle-band pairs are written as `POSSIBLE_SAME_AS` and queued |
| `er/writer.py` | Writes `(:Customer)-[:SAME_AS {probability, method, er_run_id, model_version, evidence, synthetic_link: true}]->(:SourceEntity)` and `POSSIBLE_SAME_AS` with parameterized `UNWIND`/`MERGE`. Like the loader, it **fails loudly** if a previous ER run's edges exist; the only way to delete them is `make er-reset`, which asks you to confirm |
| `er/run.py` | `make er`: normalize → block → score → adjudicate (or queue) → write → print summary |
| `eval/er_eval.py` | `make er-eval`: metrics below, written to `eval/results/er_<timestamp>.json` plus Markdown |

Excluded from the ER features **on purpose**:
- **Customer addresses:** they are synthetic and share no source addresses, so they carry no signal by construction.
- **Accounts and transactions:** seed selection is 50% laundering-linked, so these would exploit the
  design of the seeding rather than resolve identities (ADR 005).

The shared-address down-weighting noted in ADR 006 applies only if SourceEntity ↔ SourceEntity ER
is added later. It would use frequency-based down-weighting of address tokens.

## Evaluation
**Steps:**
- **Step A:** Splink + embeddings only, with middle-band pairs queued. Reports the baseline F1.
- **Step B:** LLM adjudication of middle-band pairs only, through the Anthropic API. It starts only
  after a cost estimate, and with a hard spend cap enforced in config. F1 is reported with and
  without the LLM. No local vLLM.

**Ground truth:**
- **Positives:** the 1,125 `seeded_match` pairs.
- **Hard negatives:** the 375 look-alikes, each paired with the record it imitates.
- **Negatives:** every other pair.
- **Split:** 50/50 dev/test, **grouped by `source_entity_id`** (no source entity in both halves; a
  plain customer is its own group) and **stratified by role and `seed_reason`**. All tuning happens
  on dev. The test split is scored **once**, for the final numbers.

**Metrics** (bootstrap 95% CIs):
1. **Blocking:** pair completeness (recall of true pairs among candidates), reduction ratio, and number of candidates.
2. **Pair level:** precision, recall and F1 at the chosen threshold, plus AUPRC.
3. **Customer level (screening):** hits@1 and hits@5. A customer counts as "flagged" if any pair is ≥ threshold.
4. **Breakdowns:** by source (OpenSanctions / ICIJ), number of edits (0, 1, 2), edit type, alias versus primary name, person versus organization, and laundering-linked versus random seeds.
5. **Look-alikes:** the false-positive rate on look-alikes. A look-alike resolved to its imitated record counts as a failure.
6. **Gray zone:** its size and composition, which also drives the LLM cost estimate.

**Baselines:** (a) exact match on the normalized name; (b) rapidfuzz token-set ratio with a threshold;
(c) Splink without embeddings; (d) the full model.

**Comparison with the leakage baseline (required):** the Phase 1 leak classifier, measured by
`make leak-test`, scores *customers* from customer-only fields and never sees a source record. The
ER system must be shown to do better than that:
- **Same task, same customers:** the customer-level ER score (the best pair probability) is compared
  with the leak classifier's out-of-fold score. Both report ROC-AUC and AP for seeds vs plain
  customers and for seeds vs look-alikes, on the test split. The leak classifier is supervised, so
  that comparison favours it.
- **Added value beyond leakage:** a logistic regression on [leak score] versus [leak score + ER
  score], with a likelihood-ratio test and the AUC gain. ER recall is also reported within strata of
  leak score; in particular, ER must find seeds the leak classifier ranks as unremarkable.
- **Something leakage cannot do:** pick the *correct* source entity (hits@1). The leak classifier has
  no way to answer that, so it is reported as a separate part of the ER result.

All numbers come from `make er-eval`. The README reports them with the leakage-baseline AUC next to them.

## Tests
- **Unit tests:**
  - normalization: diacritics, suffix classes, initials, token order, non-Latin input
  - phonetic keys
  - blocking: keeps the seeded pair in a fixture and drops frequent-token blocks
  - Splink: training and scoring on a tiny fixture, with deterministic output for a fixed seed
  - adjudicator: disabled by default, and makes no call without approval
  - metric functions, checked against hand-computed examples
- **Guard test:** ER modules (`er/`) never refer to ground-truth paths or labels, the same check
  that already exists for `graph/`.
- **Integration (test Neo4j):**
  - `SAME_AS` is written only at or above the threshold
  - edges carry a run ID and `synthetic_link=true`
  - a second `make er` fails loudly while a previous run's edges exist
  - `er-reset` removes only ER edges
- **Qdrant:** an integration test against a throwaway Qdrant container, like the `neo4j-test` profile.

## Dependencies to add (all free, open source)
| Package | License | Why |
| --- | --- | --- |
| splink 5.0.0 + duckdb | MIT / MIT | probabilistic record linkage |
| rapidfuzz | MIT | string similarity |
| jellyfish | MIT | phonetic keys |
| anyascii | ISC | transliteration. **CLAUDE.md lists `unidecode`, but it is GPL-2.0+.** I propose `anyascii` instead |
| sentence-transformers (+ torch) | Apache-2.0 | embeddings. torch is a one-time download of about 1 GB |
| `BAAI/bge-small-en-v1.5` | MIT | embedding model, about 130 MB from Hugging Face, no account needed |

Splink 5 is new. I'll check its API against the installed version before writing code, rather than assuming Splink 4's.

## Risks
- **Exact copies inflate recall:** about 30% of seeds have 0 edits, which makes them trivial matches.
  Results are reported per edit count so this can't hide.
- **Generated names can resemble real records:** plain and look-alike names are built from watchlist
  vocabulary, so some may genuinely look like real records that weren't seeded. These count as false
  positives; I'll report a sample for manual review.
- **Real names in the output:** every `SAME_AS` edge points from a synthetic customer to a real
  record. Edges carry `synthetic_link=true`, and Phase 3 answers must say that the link is synthetic
  and is not evidence about the real party.
- **Alias quality:** OpenSanctions aliases vary a lot in quality, and many are other-language
  transliterations. Alias matching is reported separately.
- **Residual leakage:** if the final leak-test AUC is above 0.60 and comes only from watchlist
  vocabulary, ER has to clear that AUC by a margin backed by confidence intervals, not merely match it.

## Decisions (approved 2026-10-19)
1. Dependencies approved, with `anyascii` in place of GPL `unidecode`.
2. LLM adjudication in two steps (A: none; B: Anthropic API after a cost estimate, with a hard spend cap).
3. Scope: customer ↔ source entity only.
4. Split: 50/50, grouped by `source_entity_id`, stratified by role and `seed_reason`, test scored once.
