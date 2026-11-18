"""Investigation cases, SAR drafts and analyst dispositions, with an append-only audit log.

`(:Case {case_id, alert_id, status, ...})-[:FOR_ALERT]->(:Alert)` and
`(:AuditEvent {event_id, case_id, action, analyst, at, ...})-[:ON_CASE]->(:Case)`.

The copilot recommends; the analyst decides. Nothing here closes a case or files a SAR on its own:
every disposition needs an analyst name and a written rationale, and filing needs a SAR draft that
an analyst has saved (reviewed). "File SAR" records the analyst's decision; no report is sent
anywhere. Each action writes one `AuditEvent`, and no code path updates or deletes an event.

These are the only writes the API makes. They use fixed, parameterized Cypher; LLM-generated
Cypher stays read-only and cannot name these labels (`text2cypher`).
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from neo4j import Driver, ManagedTransaction, NotificationMinimumSeverity, unit_of_work

from tracegraph.generation.sar_drafter import DRAFT_LABEL

Status = Literal["open", "escalated", "closed", "sar_filed"]
Decision = Literal["close", "escalate", "file_sar"]
FINAL_STATUSES: frozenset[str] = frozenset({"closed", "sar_filed"})
NEXT_STATUS: dict[str, Status] = {"close": "closed", "escalate": "escalated",
                                  "file_sar": "sar_filed"}  # fmt: skip
# Which decisions each status allows. Final statuses allow none.
ALLOWED: dict[str, frozenset[str]] = {
    "open": frozenset({"close", "escalate", "file_sar"}),
    "escalated": frozenset({"close", "file_sar"}),
    "closed": frozenset(),
    "sar_filed": frozenset(),
}


class CaseError(Exception):
    """A request that the case's state does not allow (HTTP 409), or an unknown case (404)."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def case_id_for(alert_id: str) -> str:
    """One case per alert: `alert-<hex>` becomes `case-<hex>`."""
    return "case-" + alert_id.removeprefix("alert-")


def labeled(text: str) -> str:
    """The draft text, starting with the DRAFT label (added if an edit removed it)."""
    body = text.strip()
    return body if body.startswith(DRAFT_LABEL) else f"{DRAFT_LABEL}\n\n{body}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_CASE_FIELDS = """c {.case_id, .alert_id, .status, .opened_at, .opened_by, .updated_at,
    .sar_draft, .sar_draft_source, .sar_draft_updated_at, .sar_draft_updated_by,
    .sar_reviewed_by, .sar_reviewed_at, .decision, .decided_by, .decided_at, .rationale}"""

# `seq` orders a case's events exactly (timestamps have one-second precision).
_EVENT = """SET c.event_seq = coalesce(c.event_seq, 0) + 1
CREATE (e:AuditEvent {event_id: $event_id, case_id: c.case_id, seq: c.event_seq,
    action: $action, analyst: $analyst, at: $at, from_status: $from_status, to_status: c.status,
    rationale: $rationale, details: $details})-[:ON_CASE]->(c)"""

_OPEN = f"""
MATCH (al:Alert {{alert_id: $alert_id}})
MERGE (c:Case {{case_id: $case_id}})
ON CREATE SET c.alert_id = $alert_id, c.status = 'open', c.opened_at = $at,
              c.opened_by = $analyst, c.updated_at = $at, c.created_now = true
MERGE (c)-[:FOR_ALERT]->(al)
WITH c, coalesce(c.created_now, false) AS created
REMOVE c.created_now
RETURN {_CASE_FIELDS} AS case, created
"""

_LOCK = f"MATCH (c:Case {{case_id: $case_id}}) SET c._lock = true REMOVE c._lock " \
        f"RETURN {_CASE_FIELDS} AS case"  # fmt: skip

_SAVE_DRAFT = f"""
MATCH (c:Case {{case_id: $case_id}})
SET c.sar_draft = $text, c.sar_draft_source = $source, c.sar_draft_updated_at = $at,
    c.sar_draft_updated_by = $analyst, c.updated_at = $at,
    c.sar_reviewed_by = $reviewed_by, c.sar_reviewed_at = $reviewed_at
WITH c
{_EVENT}
RETURN {_CASE_FIELDS} AS case
"""

_DISPOSE = f"""
MATCH (c:Case {{case_id: $case_id}})
SET c.status = $to_status, c.decision = $decision, c.decided_by = $analyst, c.decided_at = $at,
    c.rationale = $rationale, c.updated_at = $at
WITH c
{_EVENT}
RETURN {_CASE_FIELDS} AS case
"""

_OPEN_EVENT = f"""
MATCH (c:Case {{case_id: $case_id}})
{_EVENT}
"""

_GET_EVENTS = """
MATCH (e:AuditEvent {case_id: $case_id})
RETURN e {.event_id, .seq, .action, .analyst, .at, .from_status, .to_status, .rationale,
          .details} AS event
ORDER BY e.seq
"""

_LIST = f"""
MATCH (c:Case)
WHERE $status IS NULL OR c.status = $status
RETURN {_CASE_FIELDS} AS case
ORDER BY c.updated_at DESC, c.case_id SKIP $offset LIMIT $limit
"""


def _event_params(
    action: str,
    analyst: str,
    from_status: str | None,
    at: str,
    rationale: str | None = None,
    details: str | None = None,
) -> dict[str, Any]:
    return {"event_id": "evt-" + uuid.uuid4().hex, "action": action, "analyst": analyst,
            "from_status": from_status, "at": at, "rationale": rationale,
            "details": details}  # fmt: skip


@dataclass
class CaseStore:
    driver: Driver
    database: str
    timeout_seconds: float

    def _session(self) -> Any:
        # Fields of a case are absent until set; the "unknown property" warnings are noise.
        return self.driver.session(
            database=self.database, notifications_min_severity=NotificationMinimumSeverity.OFF
        )

    def _write(self, work: Any) -> Any:
        with self._session() as session:
            return session.execute_write(unit_of_work(timeout=self.timeout_seconds)(work))

    def _read(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        @unit_of_work(timeout=self.timeout_seconds)
        def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
            return [record.data() for record in tx.run(cypher, params)]

        with self._session() as session:
            result: list[dict[str, Any]] = session.execute_read(work)
            return result

    def open_case(self, alert_id: str, analyst: str) -> tuple[dict[str, Any], bool]:
        """The alert's case, created (and audited) on first open. Returns (case, created)."""
        case_id = case_id_for(alert_id)
        at = _now()

        def work(tx: ManagedTransaction) -> tuple[dict[str, Any], bool]:
            record = tx.run(_OPEN, alert_id=alert_id, case_id=case_id, at=at,
                            analyst=analyst).single()  # fmt: skip
            if record is None:
                raise CaseError(f"No alert {alert_id}.", 404)
            if record["created"]:
                tx.run(_OPEN_EVENT, case_id=case_id,
                       **_event_params("case_opened", analyst, None, at))  # fmt: skip
            return record["case"], record["created"]

        result: tuple[dict[str, Any], bool] = self._write(work)
        return result

    def get(self, case_id: str) -> dict[str, Any]:
        rows = self._read(f"MATCH (c:Case {{case_id: $case_id}}) RETURN {_CASE_FIELDS} AS case",
                          case_id=case_id)  # fmt: skip
        if not rows:
            raise CaseError(f"No case {case_id}.", 404)
        case: dict[str, Any] = rows[0]["case"]
        return case

    def events(self, case_id: str) -> list[dict[str, Any]]:
        return [row["event"] for row in self._read(_GET_EVENTS, case_id=case_id)]

    def list(self, status: str | None, offset: int, limit: int) -> list[dict[str, Any]]:
        rows = self._read(_LIST, status=status, offset=offset, limit=limit)
        return [row["case"] for row in rows]

    def save_draft(self, case_id: str, text: str, analyst: str,
                   source: Literal["generated", "analyst"]) -> dict[str, Any]:  # fmt: skip
        """Store a SAR draft. An analyst save marks it reviewed; a new generated draft clears the
        review, so filing always follows a human read of the current text."""
        text = labeled(text)
        at = _now()
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            record = tx.run(_LOCK, case_id=case_id).single()
            if record is None:
                raise CaseError(f"No case {case_id}.", 404)
            status = record["case"]["status"]
            if status in FINAL_STATUSES:
                raise CaseError(f"Case {case_id} is {status}; its SAR draft can no longer change.")
            action = "sar_draft_generated" if source == "generated" else "sar_draft_saved"
            reviewed = source == "analyst"
            saved = tx.run(
                _SAVE_DRAFT, case_id=case_id, text=text, source=source,
                reviewed_by=analyst if reviewed else None, reviewed_at=at if reviewed else None,
                **_event_params(action, analyst, status, at,
                                details=f"sha256={digest} chars={len(text)}"),
            ).single()  # fmt: skip
            assert saved is not None
            case: dict[str, Any] = saved["case"]
            return case

        result: dict[str, Any] = self._write(work)
        return result

    def dispose(self, case_id: str, decision: Decision, analyst: str,
                rationale: str) -> dict[str, Any]:  # fmt: skip
        """Record the analyst's disposition. Final statuses cannot change, and `file_sar` needs a
        reviewed SAR draft."""
        at = _now()

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            record = tx.run(_LOCK, case_id=case_id).single()
            if record is None:
                raise CaseError(f"No case {case_id}.", 404)
            case = record["case"]
            status = case["status"]
            if decision not in ALLOWED[status]:
                raise CaseError(f"Case {case_id} is {status}; '{decision}' is not allowed.")
            if decision == "file_sar" and not case.get("sar_reviewed_by"):
                raise CaseError("Filing needs a SAR draft that an analyst has reviewed and saved.")
            saved = tx.run(
                _DISPOSE, case_id=case_id, to_status=NEXT_STATUS[decision],
                decision=decision,
                **_event_params("disposition", analyst, status, at, rationale=rationale,
                                details=f"decision={decision}"),
            ).single()  # fmt: skip
            assert saved is not None
            result: dict[str, Any] = saved["case"]
            return result

        result: dict[str, Any] = self._write(work)
        return result


def subgraph(accounts: list[dict[str, Any]], payments: list[dict[str, Any]]) -> dict[str, Any]:
    """Nodes and edges for drawing an alert: flagged accounts (with owners), their counterparties
    in the alert's payments, and one edge per payment. Built from the alert's own evidence."""
    nodes: dict[str, dict[str, Any]] = {}
    for account in accounts:
        props = account.get("props", {})
        nodes[account["id"]] = {"id": account["id"], "flagged": True,
                                "owner": props.get("owner_name"),
                                "owner_id": props.get("owner_customer_id")}  # fmt: skip
    edges = []
    for payment in payments:
        props = payment.get("props", {})
        source, target = props.get("from_account"), props.get("to_account")
        if not source or not target:
            continue
        for account_id in (source, target):
            nodes.setdefault(account_id, {"id": account_id, "flagged": False, "owner": None,
                                          "owner_id": None})  # fmt: skip
        edges.append({"id": payment["id"], "source": source, "target": target,
                      "amount": props.get("amount_paid"),
                      "currency": props.get("payment_currency"),
                      "timestamp": props.get("timestamp")})  # fmt: skip
    return {"nodes": list(nodes.values()), "edges": edges}
