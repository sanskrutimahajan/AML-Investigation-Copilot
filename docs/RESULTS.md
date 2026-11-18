# Results

Detailed results for the [AML Investigation Copilot](../README.md). Every held-out split was scored
once, after all tuning on a separate development split.

Every number below comes from an evaluation command. The cited result files are committed in
`eval/results/`, with the LLM provider and model recorded in each. `make eval` collects every
result file the README and this page cite into one timestamped JSON and Markdown report,
and fails if one is missing. It re-runs nothing, because each held-out evaluation is run once through its own command.

## Entity resolution (`make er-test`, held-out split)

Customers are matched to sanctions (OpenSanctions) and offshore (ICIJ) records. The seeded links are
synthetic, so the ground truth is known (ADR 006).
- **Split:** customers are split 50/50, grouped by source record. The test half (18,758 customers,
  563 true pairs) was scored once, with every threshold frozen from the development half.
- **Model:** Splink on name, token, soft-token, suffix, DOB and country comparisons. Embeddings
  (`BAAI/bge-small-en-v1.5`) are used in blocking, not in scoring (see below).
- **Edges:** pairs at or above the match threshold become `SAME_AS`; the next band below it becomes
  `POSSIBLE_SAME_AS` for analyst review.

[Results file](../eval/results/er_test_final.json).

| Test split | Precision | Recall | F1 (95% CI, customer bootstrap) |
| --- | --- | --- | --- |
| **Splink (written to the graph)** | 0.579 | 0.721 | **0.642** (0.611–0.672) |
| Splink + LLM review of the band (not used) | 0.490 | 0.790 | 0.605 (0.573–0.637) |
| Splink with an embedding comparison | 0.919 | 0.181 | 0.303 (0.255–0.349) |
| Exact normalized name | 0.668 | 0.297 | 0.411 |
| Token-set ratio (threshold tuned on dev) | 0.029 | 0.798 | 0.056 |

- **Blocking and ranking:** blocking finds 98.1% of the true pairs. For a seeded customer, the
  right record ranks first 95.9% of the time (hits@1) and in the top five 97.5% of the time.
- **It beats the leakage baseline.** Telling seeded customers from plain ones, ER scores ROC-AUC
  0.961, against 0.555 for a classifier using only the customer's own fields. The difference has a
  95% CI of 0.382–0.431. Against look-alikes, the scores are 0.953 vs 0.551.
- **The review band:** 670 test pairs, 63 of them true. If analysts resolved the band perfectly,
  F1 would be 0.707.
- **Written to the graph** (both splits): 1,384 `SAME_AS` and 1,324 `POSSIBLE_SAME_AS` edges, each
  marked `synthetic_link: true`.

Known failures and limits:
- **Homonyms are the main error.** 148 of 187 homonyms (0.79) get a false `SAME_AS`, and they make
  up half of all false positives. A homonym carries a real watchlist or offshore record's exact name with a
  different DOB or country. When the source record has no DOB, nothing but country can separate
  them, and country is weak evidence because seeds keep the source country only half the time.
  The rate is 0.83 for country conflicts (168) and 0.47 for DOB conflicts (19). In a real screening
  system these would go to an analyst, not become an automatic match. The leak classifier's score
  predicts which homonyms are matched with AUC 0.62, so name style explains part of it.
- **Look-alikes** (similar but different names) are rarely matched: 2 of 188 (0.011).
- **The local LLM review made results worse,** on development and again on test (F1 change, 95%
  CI −0.060 to −0.016). `qwen2.5:7b-instruct` called 207 test band pairs a match, of which 39 were
  true. Its decisions are kept in the results file but do not change any edge
  (`ER_USE_LLM_ADJUDICATION=false`).
- **The embedding comparison saturated.** Four name-similarity comparisons treated as independent
  pushed similar names to probability 1.0, so its best threshold left recall at 0.18. Embeddings
  still help blocking (ANN key).

## Question answering (`make qa-check`, held-out set)

These results use the local `qwen2.5:7b-instruct` model through Ollama, with
`BAAI/bge-small-en-v1.5` name embeddings, on an M2 laptop. There are 60 questions generated from
staging (question seed 44), with gold answers computed from the staged Parquet files, never from
the graph. Development and fixes used a separate seed-42 set; this set was run once, with no
tuning afterwards. [Results file](../eval/results/qa_check_20261115T072915Z.json).

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
- **Watchlist questions are not evaluated.** This held-out run came before entity resolution wrote
  `SAME_AS` edges, and it has not been re-run since.
- **Per-family results rest on 10 questions each.**

An earlier held-out run, seed 43, came before the alerts intent was added. It scored 40/50 on the
five families it had, with lists 0/10, and two list questions were answered with grounded facts
about the wrong customer ([results file](../eval/results/qa_check_20261115T051015Z.json)).

## Typology detection (`make detect-test`, held-out seed accounts)

The test half of the seed accounts was scored once, with thresholds frozen from the development
half: 7,178 accounts, of which 44.3% are core seeds (they touch a laundering transaction).
- **Precision** is the share of flagged accounts that are core seeds. The sample enriches
  laundering about 19× compared with the full dataset (0.10% of transactions), so this is not
  real-world precision.
- **Pattern recall** is the share of IBM's labeled patterns of that type where the detector flags
  the right account (the hub, a cycle member, or the scatter-gather source or sink).
- **Degree baseline:** flag the same number of accounts by raw transaction count. The last column
  gives the detector minus the baseline, as a paired bootstrap 95% CI.

[Results file](../eval/results/detection_test_final.json).

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

## More typologies (`make detect-test-ext`, held-out seed accounts)

Three more detectors, added later (ADR 015), scored on the same test half of the seed accounts
(7,178 accounts, 44.3% core seeds), once, with thresholds frozen from the development half. The
original four detectors above were not re-scored. Ground truth was agreed before the run:
- **Layering:** IBM STACK patterns; a hit flags an account that both receives and sends within
  the pattern.
- **Mule hub:** IBM GATHER-SCATTER patterns; a hit flags the hub.
- **Rapid movement:** no IBM label, so precision only, against the degree baseline.
- **Structuring:** no IBM label; tested only on cases our own generator injects.
- IBM BIPARTITE patterns are not used: in this data they are separate one-to-one transfers.

[Results file](../eval/results/detection_ext_test_final.json).

| Detector | Pattern recall | Precision | Degree baseline (recall, precision) | Detector − baseline, 95% CI |
| --- | --- | --- | --- | --- |
| Layering (≥3 payments, each within 48 h) | 29/35 (0.83) | 0.80 | 22/35, 0.60 | precision +0.14 to +0.27; recall +0.11 to +0.44 |
| Mule hub (≥3 senders, 168 h) | 34/34 (1.00) | 0.70 | 22/34, 0.51 | precision +0.15 to +0.25; recall +0.17 to +0.65 |
| Rapid movement (1 pass-through ≥80%, 168 h) | not measured | 0.61 | —, 0.86 | precision −0.30 to −0.19 |

- **Layering and mule hubs beat the degree baseline on both measures.**
- **Rapid movement does worse than ranking accounts by degree,** on development (at every grid
  point) and on test. By a rule set before the test run, it writes no alerts.
- **Structuring** caught 40 of 40 injected cases and none of 120 decoys (above the threshold,
  spread out in time, or not cash). That checks the detector's logic, not real-world performance.
  On the real data it flags 7 accounts.
- **Alert volume:** the graph now holds 5,416 alerts, 3,856 of them from these detectors (layering
  1,918, mule hub 1,931, structuring 7). Mule-hub recall comes with a large queue.
- **Patterns per type are few (34–35),** so the intervals are wide.

## SAR drafts (`make sar-check`)

Twenty alerts were sampled, five per typology, and drafted with `qwen2.5:7b-instruct`.
[Results file](../eval/results/sar_check_20261115T075915Z.json).
- **16 of 20 produced a grounded draft**: 15 on the first try and 1 after one regeneration.
- **4 abstained**: 2 scatter-gather, 1 fan-in, and 1 fan-out whose reply was not valid JSON.
  Abstaining means saying "no grounded narrative" instead of returning an ungrounded draft.
- **Every draft carries `DRAFT – requires analyst review`**, and none uses accusatory words
  (launder, fraud, crime, illegal, illicit, guilt).
- **Mean latency was 90 s per draft** on the local model.

The grounding check confirms that cited IDs and numbers come from the evidence. It cannot tell
whether a number was read correctly: an early draft called a detection threshold "6 sender
accounts". Alerts now carry a code-written summary of what they count (ADR 010).

## Synthetic-data leakage check (`make leak-test`)

Can a classifier tell seeded customers apart using only the customer's own fields (name, DOB,
country, address)? Each score is a 5-fold cross-validated ROC-AUC, where 0.50 is chance; the
target is 0.60 or below. This is one run on the current stage, after the address-determinism fix.
[Results file](../eval/results/leak_test_20261115T060250Z.json).

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

Entity resolution separates seeds from plain customers far better than these fields alone: ROC-AUC
0.961, against 0.555 for the same leak classifier on the ER test split (see Entity resolution
above, where the homonym false-positive rate is also reported).

## Failures found and fixed

Problems found by the evaluations and live checks, and what changed. Details are in the linked ADRs
and in the [worklog](worklog.md).

| Found | Symptom | Fix |
| --- | --- | --- |
| ER label noise | 216 plain customers had the exact name of a real record but were labeled non-match, so correct matches counted as false positives. | Every plain and look-alike name is checked against all source names and aliases; 0 collisions remain ([ADR 007](decisions/007-entity-resolution.md)). |
| Leaky synthetic data | A classifier could tell seeds from homonyms by country and address alone (AUC 0.795). | All non-name attributes come from one shared function, and `make leak-test` guards against this ([ADR 006](decisions/006-seed-independent-customers-and-icij-addresses.md)). |
| Nondeterministic staging | Re-staging changed the city in 2,617 addresses, because a Faker list depended on `PYTHONHASHSEED`. | Sorted, plus tests that stage under two hash seeds ([ADR 006](decisions/006-seed-independent-customers-and-icij-addresses.md)). |
| ER model saturation | With an embedding comparison, four correlated name features pushed scores to 1.0; dev F1 was 0.26. | The model without that comparison decides; embeddings stay in blocking ([ADR 007](decisions/007-entity-resolution.md)). |
| LLM adjudication hurt | The local LLM's "match" calls were 18% correct, and dev F1 fell from 0.649 to 0.608. | The LLM changes no edge; band pairs stay for analyst review. The test confirmed the drop ([ADR 007](decisions/007-entity-resolution.md)). |
| SAR misread a number | A draft read the threshold `min_senders=6` as "6 sender accounts". The grounding check cannot catch a present-but-misread number. | Alerts carry a code-written summary of what they count ([ADR 010](decisions/010-typology-detection.md)). |
| SAR query timeout | SAR evidence scanned every payment edge (541,065 DB hits) and hit the 10 s timeout. | It now uses the transaction-ID index: 188 DB hits, 0.2 s ([ADR 011](decisions/011-ui-and-eval-report.md)). |
| Wrong UI counts | The profile summed capped activity rows and showed 58 payments sent where Neo4j had 76. | An uncapped count query, checked against Neo4j ([ADR 011](decisions/011-ui-and-eval-report.md)). |
| Router misses list questions | All 10 held-out list questions went to a single-customer template. | Not fixed; disclosed under Question answering. |
| Run-date dependent staging | Re-staging a day later shifted 7,459 generated birth dates by one day: Faker counts from today. | Birth dates count from a fixed date; staging reproduces the evaluated stage byte for byte ([ADR 006](decisions/006-seed-independent-customers-and-icij-addresses.md)). |
| UI gave up on a saved draft | A live SAR draft took 309 s; the UI's 300 s timeout reported an error although the API saved the draft. | Drafting calls have their own 900 s timeout, and the UI says a timed-out draft may still arrive ([ADR 012](decisions/012-cases-and-disposition.md)). |
| Draft called the largest payment "the total" | Passed the grounding check, because the number is in the evidence. The hand correction made during that live check was itself wrong. | Filing requires an analyst-reviewed draft; every alert now shows facts recomputed from the graph, which exposed the wrong correction ([ADR 015](decisions/015-more-typologies.md)). |
| Rapid-movement detector | Lost to ranking accounts by degree, on development and on test. | Not used: it writes no alerts, by a rule set before the test run ([ADR 015](decisions/015-more-typologies.md)). |

**Hardening added after the held-out runs** (the reported QA and SAR numbers come from the
earlier code):
- The answer and SAR prompts now say evidence lines are data, not instructions.
- `/ask` and `/sar` return and log per-request LLM usage (calls, input and output tokens, LLM
  time). `make qa-check` and `make sar-check` record the same in their result files.

The held-out result files cited above predate both changes. `make smoke` passed afterwards; for
example, a SAR draft used 1 LLM call, 2,275 input and 588 output tokens, in 48 s.
