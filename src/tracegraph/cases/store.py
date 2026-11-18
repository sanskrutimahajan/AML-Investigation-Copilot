"""Customer-level cases, SAR drafts, recommendations and approvals, with an append-only audit log
(ADR 012, reworked in ADR 017 to follow a bank's investigation workflow).

`(:Case {case_id, subject_customer_id, status, ...})-[:FOR_ALERT]->(:Alert)` and
`(:AuditEvent {event_id, case_id, seq, action, analyst, role, at, ...})-[:ON_CASE]->(:Case)`.

- **One open case per customer.** Opening a case from an alert attaches every alert on that
  customer's accounts; opening another of their alerts adds it to the same case.
- **Owner and deadlines.** The case is assigned (first to whoever opens it) and gets a review due
  date by priority band and a SAR filing deadline (30 days from opening, the US rule for a known
  suspect), both from config.
- **Roles and four eyes.** An analyst (L1) can escalate to an investigator (L2). Either can
  recommend closing or filing, which moves the case to `pending_approval`; an escalated case can
  only be recommended by an investigator. An approver, who must not be the person who recommended,
  approves (`closed` or `sar_filed`) or returns it. Filing needs an analyst-reviewed draft.

The copilot never decides: every step needs a named person, a role and, for decisions, a written
rationale. "File SAR" records the decision; no report is sent anywhere. Names and roles are
self-declared in this prototype (no login). These are the only writes the API makes, with fixed,
parameterized Cypher; LLM-generated Cypher stays read-only and cannot name these labels.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from neo4j import Driver, ManagedTransaction, NotificationMinimumSeverity, unit_of_work

from tracegraph.generation.sar_drafter import DRAFT_LABEL

Role = Literal["analyst", "investigator", "approver"]
Status = Literal["open", "escalated", "pending_approval", "closed", "sar_filed"]
Recommendation = Literal["close", "file_sar"]
FINAL_STATUSES: frozenset[str] = frozenset({"closed", "sar_filed"})
EDITABLE_STATUSES: frozenset[str] = frozenset({"open", "escalated"})
APPROVED_STATUS: dict[str, Status] = {"close": "closed", "file_sar": "sar_filed"}


class CaseError(Exception):
    """A request the case's state or the person's role does not allow (HTTP 409, or 403 for a
    role), or an unknown case or alert (404)."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def check(case: dict[str, Any], action: str, role: str, person: str,
          recommendation: str | None = None) -> None:  # fmt: skip
    """Raise CaseError unless `person` in `role` may take `action` on `case` now."""
    status = case["status"]
    if status in FINAL_STATUSES:
        raise CaseError(f"The case is {status}; no further action is possible.")
    if action == "escalate":
        if status != "open" or role != "analyst":
            raise CaseError("Only an analyst can escalate, and only an open case.", 403)
    elif action == "recommend":
        if status == "pending_approval":
            raise CaseError("A recommendation is already waiting for approval.")
        if role == "approver" or (status == "escalated" and role != "investigator"):
            raise CaseError("An escalated case is recommended by an investigator; an open case "
                            "by an analyst or investigator.", 403)  # fmt: skip
        if recommendation == "file_sar" and not case.get("sar_reviewed_by"):
            raise CaseError("Filing needs a SAR draft that an analyst has reviewed and saved.")
    elif action in ("approve", "return"):
        if status != "pending_approval":
            raise CaseError("There is no recommendation waiting for approval.")
        if role != "approver":
            raise CaseError("Only an approver can approve or return a recommendation.", 403)
        if person == case.get("recommended_by"):
            raise CaseError("Four eyes: the approver must not be the person who recommended.", 403)
    elif action == "edit_draft":
        if status not in EDITABLE_STATUSES:
            raise CaseError("The SAR draft is frozen while a recommendation is pending.")
    elif action != "assign":
        raise CaseError(f"Unknown action {action}.")


def labeled(text: str) -> str:
    """The draft text, starting with the DRAFT label (added if an edit removed it)."""
    body = text.strip()
    return body if body.startswith(DRAFT_LABEL) else f"{DRAFT_LABEL}\n\n{body}"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


_CASE_FIELDS = """c {.case_id, .subject_customer_id, .subject_name, .status, .priority,
    .priority_band, .assigned_to, .opened_at, .opened_by, .due_at, .sar_due_at, .updated_at,
    .sar_draft, .sar_draft_source, .sar_draft_alert_id, .sar_draft_updated_at,
    .sar_draft_updated_by, .sar_reviewed_by, .sar_reviewed_at, .recommendation,
    .recommended_by, .recommended_role, .recommendation_rationale, .return_status, .decision,
    .decided_by, .decided_at, .rationale, .alert_id,
    alerts: COUNT { (c)-[:FOR_ALERT]->(:Alert) }}"""

# `seq` orders a case's events exactly (timestamps have one-second precision).
_EVENT = """SET c.event_seq = coalesce(c.event_seq, 0) + 1
CREATE (e:AuditEvent {event_id: $event_id, case_id: c.case_id, seq: c.event_seq,
    action: $action, analyst: $analyst, role: $role, at: $at, from_status: $from_status,
    to_status: c.status, rationale: $rationale, details: $details})-[:ON_CASE]->(c)"""

_OWNERS = """
MATCH (al:Alert {alert_id: $alert_id})-[:FLAGS]->(a:Account)<-[:OWNS]-(cu:Customer)
RETURN cu.customer_id AS customer_id, cu.name AS name ORDER BY a.account_id
"""

# Serializes case creation per customer, then finds the customer's open case if any.
_OPEN_CASE_OF = """
MATCH (cu:Customer {customer_id: $customer_id}) SET cu._lock = true REMOVE cu._lock
WITH cu
OPTIONAL MATCH (c:Case {subject_customer_id: $customer_id})
WHERE NOT c.status IN ['closed', 'sar_filed']
RETURN c.case_id AS case_id
"""

# Every alert on the customer's accounts, with the highest priority among them.
_CUSTOMER_ALERTS = """
MATCH (:Customer {customer_id: $customer_id})-[:OWNS]->(:Account)<-[:FLAGS]-(al:Alert)
RETURN collect(DISTINCT al.alert_id) AS alert_ids, max(al.priority) AS priority
"""

_CREATE = f"""
CREATE (c:Case {{case_id: $case_id, subject_customer_id: $customer_id, subject_name: $name,
    status: 'open', priority: $priority, priority_band: $band, assigned_to: $analyst,
    opened_at: $at, opened_by: $analyst, due_at: $due_at, sar_due_at: $sar_due_at,
    updated_at: $at, alert_id: $alert_id}})
WITH c
UNWIND $alert_ids AS aid
MATCH (al:Alert {{alert_id: aid}})
MERGE (c)-[:FOR_ALERT]->(al)
WITH DISTINCT c
{_EVENT}
RETURN {_CASE_FIELDS} AS case
"""

_ATTACH = f"""
MATCH (c:Case {{case_id: $case_id}})
UNWIND $alert_ids AS aid
MATCH (al:Alert {{alert_id: aid}})
WHERE NOT (c)-[:FOR_ALERT]->(al)
MERGE (c)-[:FOR_ALERT]->(al)
WITH c, count(al) AS added
WITH c, added WHERE added > 0
SET c.updated_at = $at
{_EVENT}
RETURN added
"""

_LOCK = f"MATCH (c:Case {{case_id: $case_id}}) SET c._lock = true REMOVE c._lock " \
        f"RETURN {_CASE_FIELDS} AS case"  # fmt: skip

_UPDATE = f"""
MATCH (c:Case {{case_id: $case_id}})
SET c += $fields, c.updated_at = $at
WITH c
{_EVENT}
RETURN {_CASE_FIELDS} AS case
"""

_GET = f"MATCH (c:Case {{case_id: $case_id}}) RETURN {_CASE_FIELDS} AS case"

_CASE_ALERTS = """
MATCH (:Case {case_id: $case_id})-[:FOR_ALERT]->(al:Alert)
RETURN al.alert_id AS alert_id, al.typology AS typology, al.summary AS summary,
       al.priority AS priority, al.window_end AS window_end
ORDER BY al.priority DESC, al.window_end DESC
"""

_GET_EVENTS = """
MATCH (e:AuditEvent {case_id: $case_id})
RETURN e {.event_id, .seq, .action, .analyst, .role, .at, .from_status, .to_status,
          .rationale, .details} AS event
ORDER BY e.seq
"""

_LIST = f"""
MATCH (c:Case)
WHERE ($status IS NULL OR c.status = $status)
  AND ($assigned_to IS NULL OR c.assigned_to = $assigned_to)
RETURN {_CASE_FIELDS} AS case
ORDER BY c.priority DESC, c.updated_at DESC, c.case_id SKIP $offset LIMIT $limit
"""

_LEGACY = """
MATCH (c:Case) WHERE c.subject_customer_id IS NULL
MATCH (c)-[:FOR_ALERT]->(:Alert)-[:FLAGS]->(a:Account)<-[:OWNS]-(cu:Customer)
WITH c, cu ORDER BY a.account_id
WITH c, collect(cu)[0] AS cu
RETURN c.case_id AS case_id, c.opened_at AS opened_at, c.opened_by AS opened_by,
       cu.customer_id AS customer_id, cu.name AS name
"""


def _event(action: str, analyst: str, role: str, from_status: str | None, at: datetime,
           rationale: str | None = None, details: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"event_id": "evt-" + uuid.uuid4().hex, "action": action, "analyst": analyst,
            "role": role, "from_status": from_status, "at": at.isoformat(),
            "rationale": rationale, "details": details}  # fmt: skip


@dataclass
class CaseStore:
    driver: Driver
    database: str
    timeout_seconds: float
    sla_days: dict[str, int]  # review due date by priority band
    sar_filing_days: int
    band_cutoffs: tuple[int, int]  # (high, medium)

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

    def band(self, priority: int | None) -> str:
        high, medium = self.band_cutoffs
        p = priority or 0
        return "High" if p >= high else "Medium" if p >= medium else "Low"

    # --- opening -----------------------------------------------------------------------------

    def open_case(self, alert_id: str, analyst: str, role: str,
                  customer_id: str | None = None) -> tuple[dict[str, Any], bool]:  # fmt: skip
        """The customer's open case, with this alert attached; created if there is none.
        Returns (case, created). `customer_id` picks which owner when an alert flags several."""
        owners = self._read(_OWNERS, alert_id=alert_id)
        if not owners:
            raise CaseError(f"No alert {alert_id}, or it flags no customer's account.", 404)
        ids = [o["customer_id"] for o in owners]
        if customer_id is not None and customer_id not in ids:
            raise CaseError(f"{customer_id} owns none of the accounts alert {alert_id} flags.")
        subject = customer_id or ids[0]
        name = next(o["name"] for o in owners if o["customer_id"] == subject)
        at = _now()

        def work(tx: ManagedTransaction) -> tuple[dict[str, Any], bool]:
            existing = tx.run(_OPEN_CASE_OF, customer_id=subject).single()
            alerts = tx.run(_CUSTOMER_ALERTS, customer_id=subject).single()
            assert existing is not None and alerts is not None
            alert_ids = sorted(set(alerts["alert_ids"]) | {alert_id})
            if existing["case_id"]:
                case_id = existing["case_id"]
                tx.run(_ATTACH, case_id=case_id, alert_ids=alert_ids,
                       **_event("alerts_added", analyst, role, None, at,
                                details=f"from alert {alert_id}"))  # fmt: skip
                case = tx.run(_GET, case_id=case_id).single()
                assert case is not None
                return case["case"], False
            priority = int(alerts["priority"] or 0)
            band = self.band(priority)
            created = tx.run(
                _CREATE, case_id="case-" + uuid.uuid4().hex[:16], customer_id=subject,
                name=name, priority=priority, band=band,
                due_at=(at + timedelta(days=self.sla_days[band])).isoformat(),
                sar_due_at=(at + timedelta(days=self.sar_filing_days)).isoformat(),
                alert_id=alert_id, alert_ids=alert_ids,
                **{**_event("case_opened", analyst, role, None, at,
                            details=f"{len(alert_ids)} alerts on customer {subject}"),
                   "at": at.isoformat()},
            ).single()  # fmt: skip
            assert created is not None
            return created["case"], True

        result: tuple[dict[str, Any], bool] = self._write(work)
        return result

    # --- reads -------------------------------------------------------------------------------

    def get(self, case_id: str) -> dict[str, Any]:
        rows = self._read(_GET, case_id=case_id)
        if not rows:
            raise CaseError(f"No case {case_id}.", 404)
        case: dict[str, Any] = rows[0]["case"]
        return case

    def alerts(self, case_id: str) -> list[dict[str, Any]]:
        return [{**r, "window_end": str(r["window_end"])}
                for r in self._read(_CASE_ALERTS, case_id=case_id)]  # fmt: skip

    def events(self, case_id: str) -> list[dict[str, Any]]:
        return [row["event"] for row in self._read(_GET_EVENTS, case_id=case_id)]

    def list(self, status: str | None, assigned_to: str | None, offset: int,
             limit: int) -> list[dict[str, Any]]:  # fmt: skip
        rows = self._read(_LIST, status=status, assigned_to=assigned_to, offset=offset,
                          limit=limit)  # fmt: skip
        return [row["case"] for row in rows]

    # --- actions -----------------------------------------------------------------------------

    def _act(self, case_id: str, action: str, analyst: str, role: str,
             fields: Any, rationale: str | None = None, details: str | None = None,
             recommendation: str | None = None) -> dict[str, Any]:  # fmt: skip
        """Lock the case, check the rule, apply `fields` (a dict, or a function of the case
        returning one) and audit the action, in one transaction."""
        at = _now()

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            record = tx.run(_LOCK, case_id=case_id).single()
            if record is None:
                raise CaseError(f"No case {case_id}.", 404)
            case = record["case"]
            check(case, action, role, analyst, recommendation)
            values = fields(case) if callable(fields) else fields
            saved = tx.run(_UPDATE, case_id=case_id, fields=values,
                           **_event(action, analyst, role, case["status"], at, rationale,
                                    details)).single()  # fmt: skip
            assert saved is not None
            result: dict[str, Any] = saved["case"]
            return result

        result: dict[str, Any] = self._write(work)
        return result

    def assign(self, case_id: str, assignee: str, analyst: str, role: str) -> dict[str, Any]:
        return self._act(case_id, "assign", analyst, role, {"assigned_to": assignee},
                         details=f"assigned to {assignee}")  # fmt: skip

    def escalate(self, case_id: str, analyst: str, role: str, rationale: str) -> dict[str, Any]:
        return self._act(case_id, "escalate", analyst, role, {"status": "escalated"}, rationale)

    def recommend(self, case_id: str, recommendation: Recommendation, analyst: str, role: str,
                  rationale: str) -> dict[str, Any]:  # fmt: skip
        return self._act(
            case_id, "recommend", analyst, role,
            lambda case: {"status": "pending_approval", "recommendation": recommendation,
                          "recommended_by": analyst, "recommended_role": role,
                          "recommendation_rationale": rationale,
                          "return_status": case["status"]},
            rationale, f"recommendation={recommendation}", recommendation,
        )  # fmt: skip

    def approve(self, case_id: str, analyst: str, role: str, rationale: str) -> dict[str, Any]:
        at = _now().isoformat()
        return self._act(
            case_id, "approve", analyst, role,
            lambda case: {"status": APPROVED_STATUS[case["recommendation"]],
                          "decision": case["recommendation"], "decided_by": analyst,
                          "decided_at": at, "rationale": rationale},
            rationale, "approved",
        )  # fmt: skip

    def return_case(self, case_id: str, analyst: str, role: str,
                    rationale: str) -> dict[str, Any]:  # fmt: skip
        return self._act(
            case_id, "return", analyst, role,
            lambda case: {"status": case["return_status"] or "open", "recommendation": None,
                          "recommended_by": None, "recommended_role": None,
                          "recommendation_rationale": None},
            rationale, "returned for more work",
        )  # fmt: skip

    def save_draft(self, case_id: str, text: str, analyst: str, role: str,
                   source: Literal["generated", "analyst"],
                   alert_id: str | None = None) -> dict[str, Any]:  # fmt: skip
        """Store a SAR draft. An analyst save marks it reviewed; a new generated draft clears the
        review, so filing always follows a human read of the current text."""
        text = labeled(text)
        at = _now().isoformat()
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        reviewed = source == "analyst"
        fields: dict[str, Any] = {
            "sar_draft": text, "sar_draft_source": source, "sar_draft_updated_at": at,
            "sar_draft_updated_by": analyst, "sar_reviewed_by": analyst if reviewed else None,
            "sar_reviewed_at": at if reviewed else None,
        }  # fmt: skip
        if alert_id:
            fields["sar_draft_alert_id"] = alert_id
        action = "sar_draft_generated" if source == "generated" else "sar_draft_saved"
        return self._act(case_id, "edit_draft", analyst, role, fields,
                         details=f"{action}; sha256={digest} chars={len(text)}")  # fmt: skip

    # --- migration ---------------------------------------------------------------------------

    def attach_customer_alerts(self, case_id: str) -> int:
        """Attach every alert on the case's customer that is not attached yet (audited as
        `system`). Repairs cases migrated before migration attached them."""
        case = self.get(case_id)
        alerts = self._read(_CUSTOMER_ALERTS, customer_id=case["subject_customer_id"])[0]
        at = _now()

        def work(tx: ManagedTransaction) -> int:
            record = tx.run(_ATTACH, case_id=case_id, alert_ids=alerts["alert_ids"],
                            **_event("alerts_added", "system", "system", None, at,
                                     details="customer's other alerts, repair after "
                                     "migration")).single()  # fmt: skip
            return int(record["added"]) if record else 0

        added: int = self._write(work)
        return added

    def migrate_legacy(self) -> int:
        """Give cases from the per-alert model (ADR 012) a subject customer, an owner and
        deadlines. Nothing is deleted; each migration is audited."""
        rows = self._read(_LEGACY)
        for row in rows:
            opened = datetime.fromisoformat(row["opened_at"])
            alerts = self._read(_CUSTOMER_ALERTS, customer_id=row["customer_id"])[0]
            band = self.band(alerts["priority"])
            fields = {
                "subject_customer_id": row["customer_id"],
                "subject_name": row["name"],
                "assigned_to": row["opened_by"],
                "priority": int(alerts["priority"] or 0),
                "priority_band": band,
                "due_at": (opened + timedelta(days=self.sla_days[band])).isoformat(),
                "sar_due_at": (opened + timedelta(days=self.sar_filing_days)).isoformat(),
            }
            at = _now()

            def work(tx: ManagedTransaction, row: dict[str, Any] = row,
                     fields: dict[str, Any] = fields, at: datetime = at,
                     alert_ids: list[str] = alerts["alert_ids"]) -> None:  # fmt: skip
                tx.run(_UPDATE, case_id=row["case_id"], fields=fields,
                       **_event("migrated", "system", "system", None, at,
                                details="per-alert case moved to the customer-level model "
                                "(ADR 017)")).consume()  # fmt: skip
                # A customer case holds all of the customer's alerts.
                tx.run(
                    _ATTACH,
                    case_id=row["case_id"],
                    alert_ids=alert_ids,
                    **_event(
                        "alerts_added",
                        "system",
                        "system",
                        None,
                        at,
                        details="customer's other alerts, on migration",
                    ),
                ).consume()

            self._write(work)
        return len(rows)


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


def main() -> int:
    """`make cases-migrate`: move per-alert cases (ADR 012) to the customer-level model."""
    from neo4j import GraphDatabase

    from tracegraph.config import settings

    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password)
    )
    with driver:
        store = CaseStore(
            driver, settings.neo4j_database, settings.qa_query_timeout_seconds,
            sla_days={"High": settings.case_sla_days_high,
                      "Medium": settings.case_sla_days_medium,
                      "Low": settings.case_sla_days_low},
            sar_filing_days=settings.sar_filing_days,
            band_cutoffs=(settings.alert_priority_high, settings.alert_priority_medium),
        )  # fmt: skip
        print(f"Migrated {store.migrate_legacy()} case(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
