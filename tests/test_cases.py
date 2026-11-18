"""Case workflow rules that need no database: IDs, labels, transitions, subgraph, API validation."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from tracegraph.api.app import create_app
from tracegraph.cases.store import (
    ALLOWED,
    FINAL_STATUSES,
    CaseError,
    case_id_for,
    labeled,
    subgraph,
)
from tracegraph.config import Settings
from tracegraph.generation.sar_drafter import DRAFT_LABEL
from tracegraph.retrieval.text2cypher import static_problems


def test_one_case_per_alert_and_the_label_is_always_kept() -> None:
    assert case_id_for("alert-0123456789abcdef") == "case-0123456789abcdef"
    assert labeled("Edited text.") == f"{DRAFT_LABEL}\n\nEdited text."
    assert labeled(f"{DRAFT_LABEL}\n\nAlready labeled.") == f"{DRAFT_LABEL}\n\nAlready labeled."


def test_final_statuses_allow_no_decision_and_escalated_cannot_escalate_again() -> None:
    assert all(not ALLOWED[s] for s in FINAL_STATUSES)
    assert "escalate" not in ALLOWED["escalated"]
    assert ALLOWED["open"] == {"close", "escalate", "file_sar"}


def test_subgraph_marks_flagged_accounts_and_adds_counterparties() -> None:
    accounts = [{"id": "1:A", "props": {"owner_name": "Acme", "owner_customer_id": "cust-1"}}]
    payments = [
        {"id": "ibm-1", "props": {"from_account": "2:B", "to_account": "1:A", "amount_paid": 5.0,
                                  "payment_currency": "Euro", "timestamp": "2022-09-01T10:00"}},
        {"id": "ibm-2", "props": {"from_account": None, "to_account": "1:A"}},
    ]  # fmt: skip
    graph = subgraph(accounts, payments)
    assert {n["id"]: n["flagged"] for n in graph["nodes"]} == {"1:A": True, "2:B": False}
    assert graph["edges"] == [{"id": "ibm-1", "source": "2:B", "target": "1:A", "amount": 5.0,
                               "currency": "Euro", "timestamp": "2022-09-01T10:00"}]  # fmt: skip


def _client(store: MagicMock):  # type: ignore[no-untyped-def]
    return (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=AsyncMock()),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.CaseStore", return_value=store),
    )


def test_case_inputs_are_validated_before_the_store_is_touched() -> None:
    store = MagicMock()
    driver_patch, qdrant_patch, store_patch = _client(store)
    with driver_patch, qdrant_patch, store_patch, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        bad = [
            client.post("/cases", json={"alert_id": "alert-x", "analyst": "Dana"}),
            client.post("/cases", json={"alert_id": "alert-0123456789abcdef",
                                        "analyst": "MATCH (n) DELETE n"}),  # fmt: skip
            client.post("/cases/case-0123456789abcdef/disposition",
                        json={"analyst": "Dana", "decision": "close", "rationale": "   short   "}),
            client.post("/cases/case-0123456789abcdef/disposition",
                        json={"analyst": "Dana", "decision": "auto_close",
                              "rationale": "A long enough rationale."}),
            client.put("/cases/not-a-case/sar-draft", json={"analyst": "Dana", "text": "x"}),
            client.get("/cases", params={"status": "deleted"}),
        ]  # fmt: skip
    assert [r.status_code for r in bad] == [422] * len(bad)
    assert not store.method_calls


def test_store_rule_violations_become_http_errors() -> None:
    store = MagicMock()
    store.dispose.side_effect = CaseError("Case case-1 is closed; 'close' is not allowed.")
    store.get.side_effect = CaseError("No case case-0123456789abcdef.", 404)
    driver_patch, qdrant_patch, store_patch = _client(store)
    with driver_patch, qdrant_patch, store_patch, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        conflict = client.post("/cases/case-0123456789abcdef/disposition",
                               json={"analyst": "Dana Analyst", "decision": "close",
                                     "rationale": "No further activity found."})  # fmt: skip
        missing = client.get("/cases/case-0123456789abcdef")
    assert conflict.status_code == 409 and "not allowed" in conflict.json()["detail"]
    assert missing.status_code == 404


def test_generated_cypher_cannot_read_cases_or_the_audit_log() -> None:
    config = Settings(_env_file=None)
    for cypher in (
        "MATCH (c:Case) RETURN c.case_id AS id, 'Case' AS type, {} AS props LIMIT 5",
        "MATCH (e:`AuditEvent`) RETURN e.x AS id, 'X' AS type, {} AS props LIMIT 5",
        "MATCH (n) WHERE n.rationale IS NOT NULL RETURN n.x AS id, 'X' AS type, {} AS props "
        "LIMIT 5",
    ):
        assert "case and audit-log data cannot be queried" in static_problems(cypher, config)
    ok = "MATCH (c:Customer) RETURN c.customer_id AS id, 'Customer' AS type, {} AS props LIMIT 5"
    assert static_problems(ok, config) == []
