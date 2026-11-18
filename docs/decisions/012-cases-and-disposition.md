# ADR 012: Cases, reviewed SAR drafts and analyst disposition

- **Status:** Accepted (2026-11-18). Phase 1 of the production-shape plan ([AUDIT.md](../../AUDIT.md)).
- **Decided by the user (2026-11-18):** cases live in Neo4j and are written only by fixed endpoints;
  the analyst is a self-declared name (no authentication).

## Decisions

1. **One case per alert.** `(:Case {case_id})-[:FOR_ALERT]->(:Alert)`, with `case-<hex>` derived
   from `alert-<hex>`. Opening an existing case returns it unchanged.
2. **The copilot recommends; the analyst decides.**
   - Statuses: `open` → `escalated` → `closed` or `sar_filed`. `closed` and `sar_filed` are
     final; an escalated case cannot be escalated again.
   - Every disposition needs an analyst name and a rationale of at least 10 characters.
   - `file_sar` needs a SAR draft that an analyst has saved. A newly generated draft clears the
     review, so filing always follows a human read of the current text.
   - "File SAR" records the decision only. Nothing is sent anywhere.
3. **Append-only audit log.** Each action (`case_opened`, `sar_draft_generated`,
   `sar_draft_saved`, `disposition`) writes one `AuditEvent` with analyst, UTC time, status
   before and after, rationale, and for drafts a SHA-256 prefix and length. A per-case `seq` orders
   events exactly, since timestamps have one-second precision. No code path updates or deletes an
   event.
4. **Writes are separate from Q&A.** `tracegraph.cases.store` is the only writer, with fixed,
   parameterized Cypher in write transactions with a timeout. Generated Cypher stays read-only and
   is refused if it names case or audit labels, relationship types or properties. Graph-load
   verification ignores case data.
5. **The DRAFT label is kept.** A saved draft without `DRAFT – requires analyst review` gets it
   prepended.
6. **UI.** A Casework page with the alert subgraph (Graphviz), an editable draft, a disposition
   form that also asks the analyst to confirm review, and the audit trail. Drafting calls have
   their own timeout (`ui_draft_timeout_seconds`, 900 s).

## Evidence

- Unit tests: input validation before any store call, rule violations mapped to HTTP 409/404, the
  text-to-Cypher block, subgraph building, page tests for the case view, the analyst-name gate and
  the disposition checks. Integration test (`tests/integration/test_case_workflow.py`): one case
  from alert to filed SAR against the throwaway Neo4j, including the refusals.
- Totals at acceptance: 227 unit and 24 integration tests pass; lint clean; `make smoke` passes.
- **Live check** (native API and UI, real graph, local `qwen2.5:7b-instruct`): opened
  `case-a55f9f498b89fca8` for a FAN-IN alert (16 senders), generated a draft, corrected it, saved it
  as reviewed and escalated it. The audit trail shows the four actions in order.
- **Found in the live check:**
  - The draft took 309 s, past the UI's old 300 s timeout. The API saved it, but the UI showed an
    error. Fixed with the separate drafting timeout and a message that a timed-out draft may still
    arrive.
  - The draft called the largest payment (243,308,041.20 Yen) "the total amount". The grounding
    check passed it because the number is in the evidence. This is the known present-but-misread
    number limit (ADR 010), and the reason filing requires an analyst-reviewed draft. The analyst
    corrected it before saving.

## Limits

- No authentication: the analyst name is self-declared, so the audit trail records who the user
  says they are.
- Audit events are append-only by code, not by database permission; anyone with Neo4j write
  access could change them.
