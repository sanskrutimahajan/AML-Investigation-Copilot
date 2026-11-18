# ADR 010: Typology detection, alerts and SAR drafts (Phase 4)

- **Status:** Accepted (2026-11-15)

## Context
The IBM sample keeps every laundering pattern but enriches laundering about 19× (ADR 005). The
ground truth has 3,209 pattern transactions in 8 typologies. Profiling the patterns before design
gave:
- **Fan-in hubs** receive from a median of 8 distinct senders; **fan-out hubs** pay 7 receivers.
- **Cycles** have 2 to 12 hops; all 54 close, and 49 are time-ordered from their earliest payment.
- **Scatter-gather** patterns use about 7 intermediates.
- **Patterns last 3–4 days** (90th percentile about 95 h).
- **For comparison,** 95% of benign seeds have at most 4 distinct senders and 5 receivers.

## Decisions (user-approved evaluation method, 2026-11-15)
1. **Four detectors** (`detection/patterns.py`), pure functions over `SENT` edges read from the
   graph with a read-only query. Labels are never read.
   - **Fan-in / fan-out:** at least *k* distinct counterparties in a sliding *W*-hour window.
   - **Cycle:** a simple, time-ordered cycle of at most *L* payments within *W* hours, with one
     alert per distinct set of accounts. Without that de-duplication, repeated payments gave 3,959
     alerts on 1,104 accounts.
   - **Scatter-gather:** a source pays at least *k* distinct intermediates that each pay the same
     sink within *W* hours.
2. **Alerts** (`detection/alerts.py`):
   - `(:Alert)-[:FLAGS]->(:Account)` with a stable `alert_id`, the evidence transactions, the
     thresholds used, and a code-written `summary` of what the score counts.
   - The summary exists because a first SAR draft read the threshold `min_senders=6` as "6 sender
     accounts" when the alert had 15.
   - The writer fails loudly on a rerun; `make alerts-reset` is the only delete.
   - Alerts, like ER edges, are excluded from the loader's stale-graph verification.
3. **Evaluation** (`eval/detection_eval.py`, ADR 005):
   - **Population:** seed accounts only, split 50/50 (`make detect-split`), stratified by
     core/benign and grouped by connected component of shared laundering transactions.
   - **Tuning:** *k*, *W* and *L* are tuned on dev over a fixed grid, by F1 of pattern recall and
     precision. Test is scored once, with thresholds frozen from the dev report.
   - **Measures:** pattern recall in the right role (the hub; any cycle member; the scatter-gather
     source or sink), and precision as the core share of flagged seeds next to the 44.3% base rate.
   - **Degree baseline:** flag the same number of seeds by degree; a paired group bootstrap gives
     95% CIs for the detector minus the baseline.
4. **Q&A:** an `alerts` intent and template. "No alert" answers are written by code, and a fixed
   notice says that alerts are pattern matches in synthetic data, not findings.
5. **SAR drafts** (`generation/sar_drafter.py`, `POST /sar`):
   - The evidence is the alert, its accounts and owners, and its payments.
   - Drafts go through the same grounding check, regeneration and abstention as answers.
   - Every draft starts with `DRAFT – requires analyst review`, and the fixed notices are added.
   - `make sar-check` reports grounding and labeling.

## Evidence
- **Dev choice** (`detection_dev_20261115T065128Z`):
  - fan-in k=6, W=168 h; fan-out k=7, W=96 h; cycle L=12, W=96 h; scatter-gather k=3, W=96 h.
  - Fan-in W and cycle L sit at the edge of the grid. The grid was not widened after seeing this:
    12 is the longest labeled cycle, and 168 h is a week of an 18-day dataset.
- **Test, scored once** (`detection_test_final.json`; 7,178 seeds, base rate 44.3%):

  | Detector | Pattern recall | Precision | Degree baseline (recall, precision) | Detector − baseline, 95% CI |
  | --- | --- | --- | --- | --- |
  | Fan-in | 15/22 (0.68) | 0.79 | 10/22, 0.53 | precision +0.17 to +0.36; recall −0.12 to +0.69 |
  | Fan-out | 11/27 (0.41) | 0.57 | 5/27, 0.51 | precision −0.02 to +0.13; recall −0.09 to +0.60 |
  | Cycle | 23/24 (0.96) | 0.89 | 17/24, 0.84 | precision −0.00 to +0.11; recall +0.03 to +0.53 |
  | Scatter-gather | 16/17 (0.94) | 0.98 | 3/17, 0.81 | precision +0.03 to +0.32; recall +0.46 to +1.00 |

  - Scatter-gather beats the baseline on both measures, fan-in on precision, and cycles on recall.
  - **Fan-out does not clearly beat it.** Its recall fell from 16/21 on dev to 11/27 on test, so
    the dev choice fit dev data too closely. Reported as it came out.
- **Graph:** 1,560 alerts (fan-in 658, fan-out 396, cycle 470, scatter-gather 36) and 2,301
  `FLAGS` edges. Recomputing from the staged transactions gives the identical 1,560 alert IDs.
- **Alerts in Q&A** (held-out QA seed 44, run once; `qa_check_20261115T072915Z`): the alerts
  family scored 9/10. The miss cited one of an account's two alerts.
- **SAR drafts** (`sar_check_20261115T075915Z`, 5 alerts per typology):
  - 16 of 20 were grounded: 15 on the first try and 1 after a regeneration.
  - 4 abstained: 2 scatter-gather, 1 fan-in, and 1 fan-out on invalid JSON.
  - All were labeled, none used accusatory words, and mean latency was 90 s.
  - Limitation: grounding checks that numbers are present, not what they mean (hence `summary`).
- **Tests:** detector unit tests (windows, thresholds, time order, de-duplication, scatter-gather
  timing, stable IDs), evaluation helpers, SAR drafter and alert answers with a scripted LLM, and
  integration tests for the writer, reset scope, re-load verification and both templates.
