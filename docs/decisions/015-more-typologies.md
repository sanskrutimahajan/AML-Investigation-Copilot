# ADR 015: Layering, mule-hub, rapid-movement and structuring detectors

- **Status:** Accepted (2026-11-18). Phase 4 of the production-shape plan ([AUDIT.md](../../AUDIT.md)).
- **Decided by the user (2026-11-18):** the ground truth and scoring below, after an earlier
  proposal was corrected (IBM BIPARTITE patterns turned out to be one-to-one transfers).

## Decisions

1. **Four more detectors** in `detection/patterns.py`, pure functions of the edge table like the
   original four (ADR 010). The edge table now carries amounts, currencies and payment format.
   - **Layering:** a time-ordered chain of at least `L` payments, each paid on within `W` hours.
   - **Mule hub:** at least `k` distinct senders within `W` hours, then money out within `W` hours.
   - **Rapid movement:** at least `n` receipts each paid on (≥80%, same currency) within `W` hours.
   - **Structuring:** at least `k` cash payments just under a reporting threshold (10,000 USD,
     band 10%) within 72 hours. Fixed by config, not tuned.
2. **Ground truth and scoring** (`eval/detection_ext_eval.py`, same population, split, baseline and
   bootstrap as ADR 010):
   - layering against IBM STACK, a hit being an account that both receives and sends;
   - mule hubs against IBM GATHER-SCATTER hubs;
   - rapid movement by precision only, against the degree baseline;
   - structuring only on cases injected by our own generator, with three kinds of decoy;
   - IBM BIPARTITE not used.
3. **The frozen test result of the original detectors is untouched.** The new detectors have their
   own dev reports and their own once-only test file.
4. **Rule set before the test run:** a new detector writes alerts only if its test precision-gain
   95% CI is above 0. Structuring, which has no real labels, writes alerts on the logic check.
5. **Alerts are added by typology.** `make alerts` writes only typologies that have no alerts yet
   and still fails loudly if all are present. `alerts-reset` refuses while cases exist, since
   deleting alerts would cut cases off from their evidence.
6. **Each alert can be re-checked.** `ALERT_FACTS` re-reads the alert's own payments with a fixed,
   read-only query (senders, receivers, payments out, time span, amounts, formats). The API returns
   it with every alert and case, and the workbench shows it as "Re-checked in the graph".

## Evidence (test split, scored once; `eval/results/detection_ext_test_final.json`)

7,178 seed accounts, 44.3% core seeds. Thresholds from `detection_ext_dev_20261118T135055Z`.

| Detector | Pattern recall | Precision | Degree baseline | Detector − baseline, 95% CI |
| --- | --- | --- | --- | --- |
| Layering (3, 48 h) | 29/35 | 0.80 | 22/35, 0.60 | precision +0.14 to +0.27; recall +0.11 to +0.44 |
| Mule hub (3, 168 h) | 34/34 | 0.70 | 22/34, 0.51 | precision +0.15 to +0.25; recall +0.17 to +0.65 |
| Rapid movement (1, 168 h) | — | 0.61 | 0.86 | precision −0.30 to −0.19 |

- Structuring: 40/40 injected cases, 0/120 decoys; 7 real accounts flagged.
- Rapid movement lost to the baseline at every dev grid point too; it was not redesigned and
  writes no alerts.
- Alerts written: layering 1,918, mule hub 1,931, structuring 7 (5,416 alerts in all).
- Tests: detector unit tests, eval-report summary test, integration tests for the facts query,
  additive alert writes and the reset guard. 235 unit and 26 integration tests pass; lint clean.
- Live: the containerized API serves all seven typologies and the facts. On the demo case the
  facts showed that a hand correction made during the Phase 1 live check was itself wrong (it
  copied the draft's smallest payment, 170,384.96 Yen; the graph has 49,459.70 Yen). It was
  corrected, and the audit trail shows the correction.

## Limits

- Mule-hub recall comes with 1,931 alerts; the analyst queue more than triples.
- 34–35 patterns per type, so the intervals are wide.
- Structuring's numbers test the detector's logic on our own injected cases, not real performance.
- The facts are computed from the alert's own payments only, not the account's wider activity.
