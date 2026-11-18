# ADR 016: Data-quality checks and alert replay; no simulated live feed

- **Status:** Accepted (2026-11-18). Phase 6 of the production-shape plan ([AUDIT.md](../../AUDIT.md)).
- **Decided by the user (2026-11-18):** no simulated live feed; data-quality handling plus a
  read-only replay view instead.

## Decisions

1. **No live feed.** Generated payments would carry no realistic laundering, so their alerts would
   be noise or would catch patterns the generator planted. Writing them into the graph every
   evaluation reads would also put the reported results at risk.
2. **Data-quality check on the IBM feed** (`ingest/ibm_aml.quality_check`):
   - rows with a missing or unparseable field, or a non-positive amount, are dropped before
     sampling;
   - exact duplicates stay in the sampling pool, so the seeded random sample is unchanged, and are
     dropped from the staged sample;
   - rows out of time order are counted (the pipeline sorts by time);
   - counts go into the stage manifest, and rows with reasons into
     `data/processed/quarantine_transactions.parquet`.
3. **Replay view.** An "as of" date in the workbench sidebar shows the overview and queue as they
   stood at the end of that day: only alerts whose window had ended (`$as_of` in `ALERTS_QUEUE`
   and `ALERT_SUMMARY`, `as_of` on `/alerts/queue` and `/alerts/summary`). Read-only; it adds no
   data.

## Evidence

- Real data: 0 invalid rows, 9 duplicate rows (none sampled), 2,418,736 rows out of time order.
  Re-staging left every staging and ground-truth file byte-identical, after the birth-date fix
  below.
- **Found while verifying:** generated birth dates depended on the run date (Faker counts from
  today). Fixed and recorded in the [ADR 006 update](006-seed-independent-customers-and-icij-addresses.md).
- Replay on the real graph: 954 alerts by 2022-09-02, 2,441 by 2022-09-05, 5,082 by 2022-09-10,
  5,416 in all; the workbench showed the same 2,441 for 2022-09-05.
- Tests: quality-check unit test; replay tests for the API, the real query (integration) and the
  workbench. 239 unit and 27 integration tests pass; lint clean.

## Limits

- The duplicate check matches rows identical in every field; two genuine identical payments in the
  same minute would also be treated as duplicates.
- Replay filters the overview and queue only; a subject's alert list shows all alerts.
