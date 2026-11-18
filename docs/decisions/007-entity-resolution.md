# ADR 007: Entity resolution (Phase 2)

- **Status:** Accepted (2026-11-18). The test split was scored once
  (`eval/results/er_test_final.json`); the edges are written.
- **Settled in the approved plan**
  ([docs/plans/phase2-entity-resolution.md](../plans/phase2-entity-resolution.md)):
  - scope: customer ↔ source entity only;
  - dependencies, with anyascii in place of GPL unidecode;
  - LLM adjudication in two steps (the Anthropic API, cost estimate and spend cap are superseded by
    [ADR 008](008-llm-provider.md): local Ollama, nothing billed);
  - a 50/50 split grouped by `source_entity_id` and stratified by role and seed reason, with the
    test split scored once;
  - seed 42 as the official seed.

## Decisions

1. **Label noise: plain and look-alike names never collide with a real record.** They are checked
   against the (kind, `name_signature`) of every source record and alias, on the final name after
   edits. On the current stage, 0 of 35,640 plain customers and 0 of 375 look-alikes share a
   normalized name with a source record; only seeds and homonyms do, by design.
2. **Homonym stratum and shared attributes.**
   - 375 homonyms (`homonym_rate` 0.01) carry a real record's exact name, with a conflicting DOB or
     country drawn from the shared attribute rule.
   - Every non-name attribute, for every role, comes from one function.
   - Chi-square and KS tests check attribute independence across roles.
3. **ICIJ sampling determinism:** the `unique()` mask misalignment is fixed, and the determinism
   test fails with the bug and passes with the fix.
4. **Leakage baseline.**
   - Four comparisons moved more than ±0.025 after the ICIJ fix. Ten evaluation draws showed this
     was a real data change, not noise; most comparisons became less leaky. The user accepted the
     corrected stage (2026-11-15).
   - After the address fix (ADR 006), one run gave a maximum of 0.621 (homonyms vs plain, GB). That
     is above the 0.60 target and within the 0.625 ceiling of the homonym judgment call. Every
     comparison involving seeds is at most 0.569.
5. **The decision model is Splink without the embedding comparison**
   (`ER_DECISION_VARIANT=splink_no_embeddings`, user decision 2026-11-18).
   - On dev, the full model saturated: 23,957 pairs scored ≥ 0.92 (8,822 without embeddings), and
     the best-F1 threshold was 1.0, where recall was 0.15 (F1 0.260 vs 0.649).
   - The likely cause is four name-similarity comparisons (Jaro-Winkler, token Jaccard, soft
     tokens, embedding cosine) counted as independent evidence, each worth about 15–19 bits.
   - Embeddings stay in blocking (the ANN key).
6. **Thresholds from dev:** match 0.999779 (best F1); review 0.991478, the lowest threshold whose
   band holds at most 1,000 dev pairs (654).
7. **The LLM review does not change edges** (`ER_USE_LLM_ADJUDICATION=false`, user decision
   2026-11-18). Band pairs stay `POSSIBLE_SAME_AS` for analyst review.
   - On dev, the local LLM lowered F1 from 0.649 to 0.608 (paired 95% CI of the change −0.058 to
     −0.023): it called 206 band pairs a match, of which 37 were true.
   - All 1,324 band pairs were still adjudicated (0 failures, 0 JSON retries, about 7.8 s per pair),
     so the test file reports the comparison.
8. **`er-test` checks** that the dev report was tuned for the configured decision variant and
   thresholds, and that every band pair has an LLM decision, before it scores the test split once.

## Evidence (test split, scored once)

18,758 customers, 563 true pairs. Model: Splink; embeddings `BAAI/bge-small-en-v1.5`; LLM
`qwen2.5:7b-instruct` (Ollama, prompt v2).

| Test | Precision | Recall | F1 (95% CI) |
| --- | --- | --- | --- |
| Splink, written | 0.579 | 0.721 | 0.642 (0.611–0.672) |
| Splink + LLM (not used) | 0.490 | 0.790 | 0.605 (0.573–0.637) |
| Splink with embeddings | 0.919 | 0.181 | 0.303 (0.255–0.349) |
| Exact name | 0.668 | 0.297 | 0.411 |
| Token-set ratio | 0.029 | 0.798 | 0.056 |

- **Blocking and ranking:** blocking pair completeness 0.981; hits@1 0.959; hits@5 0.975.
- **Review band:** 670 pairs, 63 true; F1 0.707 if resolved perfectly.
- **The LLM still lowers F1 on test:** F1 change 95% CI −0.060 to −0.016; 207 band pairs called a
  match, 39 true.
- **Leakage:** customer-level ROC-AUC, seeds vs plain, is 0.961 for ER vs 0.555 for the leak
  classifier (difference 95% CI 0.382–0.431). Seeds vs look-alikes: 0.953 vs 0.551.
- **Homonyms:** false `SAME_AS` rate 0.791 (148 of 187), half of all false positives. It is 0.827
  for country conflicts (168) and 0.474 for DOB conflicts (19). The leak score predicts which
  homonyms are matched with AUC 0.624, so name style explains part of the rate. Disclosed in the
  README.
- **Look-alikes:** 2 of 188 matched to any record (0.011); none to the imitated record.
- **Graph:** 1,384 `SAME_AS` and 1,324 `POSSIBLE_SAME_AS` edges (both splits), method
  `splink_no_embeddings`, all `synthetic_link: true`. A second `er-write` is refused.
- **Process notes:**
  - The 2026-11-17 re-stage changed only `customers.parquet`; customer names were identical, and
    the 1,560 alerts were rewritten with identical IDs.
  - Qdrant's approximate search found 375,003 ANN candidates vs 375,002 in October; it is not
    exactly repeatable.
