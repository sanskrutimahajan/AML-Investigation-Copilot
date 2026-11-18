"""Case workflow rules that need no database: roles and four eyes, priority, scenarios, subgraph,
and API validation (ADR 012, ADR 017)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tracegraph.api.app import create_app
from tracegraph.cases.store import CaseError, check, labeled, subgraph
from tracegraph.config import Settings
from tracegraph.detection import scenarios
from tracegraph.detection.alerts import priority
from tracegraph.generation.sar_drafter import DRAFT_LABEL
from tracegraph.retrieval.text2cypher import static_problems


def _case(status: str, **extra: object) -> dict[str, object]:
    return {"status": status, **extra}


def test_analyst_escalates_and_recommends_but_never_approves() -> None:
    check(_case("open"), "escalate", "analyst", "Dana")
    check(_case("open"), "recommend", "analyst", "Dana", "close")
    for action in ("approve", "return"):
        with pytest.raises(CaseError):
            check(_case("pending_approval", recommended_by="Sam"), action, "analyst", "Dana")
    with pytest.raises(CaseError, match="escalate"):
        check(_case("open"), "escalate", "investigator", "Sam")


def test_an_escalated_case_is_recommended_only_by_an_investigator() -> None:
    check(_case("escalated"), "recommend", "investigator", "Sam", "close")
    with pytest.raises(CaseError, match="investigator"):
        check(_case("escalated"), "recommend", "analyst", "Dana", "close")


def test_four_eyes_the_recommender_cannot_approve() -> None:
    pending = _case("pending_approval", recommended_by="Sam")
    check(pending, "approve", "approver", "Alex")
    with pytest.raises(CaseError, match="Four eyes"):
        check(pending, "approve", "approver", "Sam")


def test_filing_needs_a_reviewed_draft_and_final_cases_are_frozen() -> None:
    with pytest.raises(CaseError, match="reviewed"):
        check(_case("open"), "recommend", "analyst", "Dana", "file_sar")
    check(_case("open", sar_reviewed_by="Dana"), "recommend", "analyst", "Dana", "file_sar")
    with pytest.raises(CaseError, match="frozen"):
        check(_case("pending_approval"), "edit_draft", "analyst", "Dana")
    for status in ("closed", "sar_filed"):
        with pytest.raises(CaseError, match="no further action"):
            check(_case(status), "assign", "analyst", "Dana")


def test_priority_adds_scenario_strength_watchlist_and_history_capped_at_100() -> None:
    assert priority(15, 6, "none", 0) == (27, "scenario 15 + strength 12 + watchlist 0 + history 0")
    assert priority(25, 30, "match", 9)[0] == 95  # 25 + 20 + 30 + 20
    assert priority(30, 30, "match", 9)[0] == 100  # capped
    assert priority(20, 3, "possible match", 1)[0] == 20 + 6 + 15 + 5


def test_every_alerting_typology_has_a_scenario_with_guidance() -> None:
    from tracegraph.api.client import TYPOLOGIES

    for typology in TYPOLOGIES:
        scenario = scenarios.scenario_for(typology)
        assert scenario is not None and scenario.next_steps and scenario.red_flag
    assert scenarios.label("FAN-IN") == "TM-01 Funnel account activity"
    assert len({s.scenario_id for s in scenarios.SCENARIOS.values()}) == len(scenarios.SCENARIOS)


def test_the_label_is_always_kept() -> None:
    assert labeled("Edited text.") == f"{DRAFT_LABEL}\n\nEdited text."
    assert labeled(f"{DRAFT_LABEL}\n\nAlready labeled.") == f"{DRAFT_LABEL}\n\nAlready labeled."


def test_subgraph_marks_flagged_accounts_and_adds_counterparties() -> None:
    accounts = [{"id": "1:A", "props": {"owner_name": "Acme", "owner_customer_id": "cust-1"}}]
    payments = [
        {"id": "ibm-1", "props": {"from_account": "2:B", "to_account": "1:A", "amount_paid": 5.0,
                                  "payment_currency": "Euro", "timestamp": "2022-09-01T10:00"}},
        {"id": "ibm-2", "props": {"from_account": None, "to_account": "1:A"}},
    ]  # fmt: skip
    graph = subgraph(accounts, payments)
    assert {n["id"]: n["flagged"] for n in graph["nodes"]} == {"1:A": True, "2:B": False}
    assert len(graph["edges"]) == 1


def _client(store: MagicMock):  # type: ignore[no-untyped-def]
    return (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=AsyncMock()),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.CaseStore", return_value=store),
    )


def test_case_inputs_are_validated_before_the_store_is_touched() -> None:
    store = MagicMock()
    driver_patch, qdrant_patch, store_patch = _client(store)
    case = "/cases/case-0123456789abcdef"
    with driver_patch, qdrant_patch, store_patch, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        bad = [
            client.post("/cases", json={"alert_id": "alert-x", "analyst": "Dana",
                                        "role": "analyst"}),
            client.post("/cases", json={"alert_id": "alert-0123456789abcdef",
                                        "analyst": "Dana", "role": "admin"}),
            client.post("/cases", json={"alert_id": "alert-0123456789abcdef",
                                        "analyst": "MATCH (n) DELETE n", "role": "analyst"}),
            client.post(f"{case}/recommendation",
                        json={"analyst": "Dana", "role": "analyst", "rationale": "  short  ",
                              "recommendation": "close"}),
            client.post(f"{case}/recommendation",
                        json={"analyst": "Dana", "role": "analyst",
                              "rationale": "A long enough rationale.",
                              "recommendation": "auto_close"}),
            client.post(f"{case}/approval",
                        json={"analyst": "Alex", "role": "approver",
                              "rationale": "A long enough rationale."}),
            client.put("/cases/not-a-case/sar-draft",
                       json={"analyst": "Dana", "role": "analyst", "text": "x"}),
            client.get("/cases", params={"status": "deleted"}),
        ]  # fmt: skip
    assert [r.status_code for r in bad] == [422] * len(bad)
    assert not store.method_calls


def test_rule_violations_become_http_errors() -> None:
    store = MagicMock()
    store.approve.side_effect = CaseError("Four eyes: the approver must not be the person who "
                                          "recommended.", 403)  # fmt: skip
    store.get.side_effect = CaseError("No case case-0123456789abcdef.", 404)
    driver_patch, qdrant_patch, store_patch = _client(store)
    with driver_patch, qdrant_patch, store_patch, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        refused = client.post("/cases/case-0123456789abcdef/approval",
                              json={"analyst": "Sam", "role": "approver", "approve": True,
                                    "rationale": "Looks right to me."})  # fmt: skip
        missing = client.get("/cases/case-0123456789abcdef")
    assert refused.status_code == 403 and "Four eyes" in refused.json()["detail"]
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
