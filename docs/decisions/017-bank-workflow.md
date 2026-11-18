# ADR 017: Bank-style workflow: scenario library, alert priority, customer cases, four eyes

- **Status:** Accepted (2026-11-18). Supersedes the per-alert case model of
  [ADR 012](012-cases-and-disposition.md); the audit-log and DRAFT rules stay.
- **Decided by the user (2026-11-18):** "replicate the way it would be used in a real bank".

## Decisions

1. **Scenario library** (`detection/scenarios.py`). Each detector is shown to analysts as a scenario:
   an ID (TM-01 to TM-07), a business name ("Funnel account activity" for fan-in), what it looks
   for, why it is a red flag, and suggested checks. Code names stay in the code and result files.
2. **Alert priority** (0–100), computed once per alert and stored, with a readable breakdown:
   - scenario weight (15–30; higher where the pattern is more specific to laundering);
   - plus strength (2 × the alert's score, at most 20);
   - plus 30 for an owner's `SAME_AS` link, or 15 for `POSSIBLE_SAME_AS`;
   - plus 5 per other alert on the owners' accounts (at most 20).

   Bands: High ≥ 60, Medium ≥ 35, Low below. The queue is worked highest first.
   `make alerts` scores new alerts; `make alerts-score` re-scores after entity resolution changes.
   Scoring at read time was tried first: it timed out (10 s) over 5,416 alerts.
3. **One case per customer.** Opening a case from an alert attaches every alert on that customer's
   accounts; another alert on the same customer goes to the same open case. Case IDs are random
   (`case-<16 hex>`).
4. **Owner and deadlines.** Cases are assigned (first to whoever opens them) and get a review due
   date by band (7, 15 or 30 days, illustrative) and a SAR deadline 30 days from opening (the US
   rule when a suspect is known).
5. **Roles and four eyes.**
   - An analyst (L1) escalates to an investigator (L2) or recommends.
   - An escalated case is recommended only by an investigator.
   - A recommendation (close, or file SAR, which needs a reviewed draft) waits for an approver.
   - The approver approves or returns it, and must not be the person who recommended.
   - The draft is frozen while a recommendation waits.
6. **Migration without deletion.** `make cases-migrate` moved the one per-alert case to the new
   model; its audit trail is intact and the migration is itself audited.

## Evidence

- Priority on the real graph: 492 High, 2,506 Medium and 2,418 Low alerts. The queue loads in about
  0.5 s, and scoring all alerts takes about 2 s.
- Tests:
  - unit tests for the role and four-eyes rules, the priority rule, the scenario library and API
    validation;
  - integration tests for a full customer case from alert to an approved SAR filing (including the
    refusals), and for escalation, return and reassignment.
  - 250 unit and 28 integration tests pass; lint clean; `make smoke` passes.
- Live, against the containers: an analyst got 403 recommending an escalated case; the investigator's
  recommendation moved it to pending approval; the same person as approver got the four-eyes refusal.

## Limits

- Priority weights and review targets are judgment, not fitted or validated against outcomes.
- Names and roles are self-declared (no login), so four eyes holds only for honest users.
- An alert flagging several customers opens a case for one of them (the first flagged account's
  owner, unless chosen); the others are not opened automatically.
- SAR drafts narrate one alert at a time, not the whole case.
