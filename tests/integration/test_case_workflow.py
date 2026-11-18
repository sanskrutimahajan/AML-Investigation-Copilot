"""Full cases through the API against the throwaway Neo4j, following a bank's workflow
(ADR 017): open a customer's case from an alert, draft and review the SAR, recommend, and get a
second person's approval; plus escalation and returns. The LLM is a stand-in that returns a grounded
draft, so the tests check the workflow, not model quality."""

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from neo4j import Driver

from tests.integration.conftest import DATABASE
from tests.integration.test_alerts import _alert
from tracegraph.api.app import create_app
from tracegraph.config import Settings
from tracegraph.detection import alerts
from tracegraph.graph import loaders
from tracegraph.llm.base import Completion

pytestmark = pytest.mark.integration
ALERT_ID = "alert-0123456789abcdef"


@dataclass
class _GroundedLLM:
    """Returns one sentence citing the alert, which passes the grounding check."""

    settings: Any = None

    def complete(self, prompt: str, *, system: str | None = None,
                 json_schema: dict[str, Any] | None = None) -> Completion:  # fmt: skip
        text = json.dumps({"sentences": [{"text": "The alert flagged one receiving account.",
                                          "citations": [ALERT_ID]}]})  # fmt: skip
        return Completion(text, 0.01, "stand-in", 10, 10)


@pytest.fixture
def client(empty_graph: Driver, staged: Settings) -> Any:
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    alerts.write_alerts(empty_graph, DATABASE, _alert(staged), "run-1", 10)
    alerts.score_alerts(empty_graph, DATABASE, 10)
    config = staged.model_copy(update={"neo4j_uri": staged.neo4j_test_uri,
                                       "neo4j_database": DATABASE})  # fmt: skip
    with (
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.SentenceTransformerEmbedder"),
        patch("tracegraph.api.app.get_provider", return_value=_GroundedLLM()),
        TestClient(create_app(config)) as test_client,
    ):
        yield test_client


DANA = {"analyst": "Dana Analyst", "role": "analyst"}
SAM = {"analyst": "Sam Investigator", "role": "investigator"}
ALEX = {"analyst": "Alex Approver", "role": "approver"}
WHY = "Payments match the scenario and the customer's profile does not explain them."


def _open(client: TestClient, who: dict[str, str]) -> Any:
    return client.post("/cases", json={"alert_id": ALERT_ID, **who})


def test_customer_case_from_alert_to_an_approved_sar_filing(client: TestClient) -> None:
    opened = _open(client, DANA)
    case = opened.json()["case"]
    case_id = case["case_id"]
    assert opened.status_code == 201 and case["status"] == "open"
    assert case["subject_customer_id"].startswith("cust-") and case["alerts"] == 1
    assert case["assigned_to"] == "Dana Analyst" and case["due_at"] and case["sar_due_at"]
    # Another alert on the same customer goes to the same open case.
    again = _open(client, SAM)
    assert again.status_code == 200 and again.json()["case"]["case_id"] == case_id

    view = client.get(f"/cases/{case_id}").json()
    assert view["selected_alert_id"] == ALERT_ID and view["case_alerts"][0]["scenario"]
    assert view["alert"]["scenario"]["scenario_id"] == "TM-01"

    generated = client.post(f"/cases/{case_id}/sar-draft", json=DANA).json()
    assert generated["draft"]["status"] == "drafted"
    assert generated["case"]["sar_reviewed_by"] is None

    rec = {**DANA, "recommendation": "file_sar", "rationale": WHY}
    refused = client.post(f"/cases/{case_id}/recommendation", json=rec)
    assert refused.status_code == 409 and "reviewed" in refused.json()["detail"]

    saved = client.put(f"/cases/{case_id}/sar-draft", json={**DANA, "text": "Edited narrative."})
    assert saved.json()["case"]["sar_draft"].startswith("DRAFT – requires analyst review")

    pending = client.post(f"/cases/{case_id}/recommendation", json=rec)
    assert pending.json()["case"]["status"] == "pending_approval"
    # The draft is frozen while waiting; an analyst cannot approve; nor can the recommender.
    assert client.put(f"/cases/{case_id}/sar-draft",
                      json={**DANA, "text": "Late edit."}).status_code == 409  # fmt: skip
    assert (
        client.post(
            f"/cases/{case_id}/approval", json={**DANA, "approve": True, "rationale": WHY}
        ).status_code
        == 403
    )
    self_approval = {"analyst": "Dana Analyst", "role": "approver", "approve": True,
                     "rationale": WHY}  # fmt: skip
    four_eyes = client.post(f"/cases/{case_id}/approval", json=self_approval)
    assert four_eyes.status_code == 403 and "Four eyes" in four_eyes.json()["detail"]

    approved = client.post(f"/cases/{case_id}/approval",
                           json={**ALEX, "approve": True, "rationale": WHY})  # fmt: skip
    assert approved.json()["case"]["status"] == "sar_filed"
    assert approved.json()["case"]["decided_by"] == "Alex Approver"
    audit = approved.json()["audit"]
    assert [e["action"] for e in audit] == [
        "case_opened", "edit_draft", "edit_draft", "recommend", "approve"]  # fmt: skip
    assert [e["role"] for e in audit][-2:] == ["analyst", "approver"]
    assert (
        client.post(
            f"/cases/{case_id}/assign", json={**DANA, "assignee": "Sam Investigator"}
        ).status_code
        == 409
    )


def test_escalation_return_and_reassignment(client: TestClient) -> None:
    case_id = _open(client, DANA).json()["case"]["case_id"]
    assert (
        client.post(f"/cases/{case_id}/escalate", json={**DANA, "rationale": WHY}).json()["case"][
            "status"
        ]
        == "escalated"
    )
    assign = client.post(f"/cases/{case_id}/assign", json={**DANA, "assignee": "Sam Investigator"})
    assert assign.json()["case"]["assigned_to"] == "Sam Investigator"
    # Escalated: only an investigator recommends.
    rec = {"recommendation": "close", "rationale": WHY}
    assert client.post(f"/cases/{case_id}/recommendation",
                       json={**DANA, **rec}).status_code == 403  # fmt: skip
    assert (
        client.post(f"/cases/{case_id}/recommendation", json={**SAM, **rec}).json()["case"][
            "status"
        ]
        == "pending_approval"
    )
    returned = client.post(f"/cases/{case_id}/approval",
                           json={**ALEX, "approve": False, "rationale": WHY})  # fmt: skip
    assert returned.json()["case"]["status"] == "escalated"  # back to where it came from
    mine = client.get("/cases", params={"assigned_to": "Sam Investigator"}).json()["cases"]
    assert [c["case_id"] for c in mine] == [case_id]


def test_case_writes_stay_out_of_load_verification(empty_graph: Driver, staged: Settings,
                                                    client: TestClient) -> None:  # fmt: skip
    _open(client, DANA)
    # A repeat load with cases present still verifies (cases are analyst work, not staging data).
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    assert client.get("/cases/case-ffffffffffffffff").status_code == 404
    assert client.post("/cases", json={"alert_id": "alert-ffffffffffffffff",
                                       **DANA}).status_code == 404  # fmt: skip


def test_alerts_cannot_be_reset_while_cases_reference_them(empty_graph: Driver,
                                                           client: TestClient) -> None:  # fmt: skip
    case_id = _open(client, DANA).json()["case"]["case_id"]
    with pytest.raises(alerts.ExistingAlertsError, match="cases reference alerts"):
        alerts.reset_alerts(empty_graph, DATABASE)
    assert client.get(f"/cases/{case_id}").json()["facts"]["payments"] == 1


def test_migration_gives_a_per_alert_case_its_customer_and_all_their_alerts(
    empty_graph: Driver, client: TestClient
) -> None:
    from tracegraph.cases.store import CaseStore

    # A case in the old per-alert shape (ADR 012): no subject customer, one linked alert.
    empty_graph.execute_query(
        "MATCH (al:Alert {alert_id: $a}) CREATE (c:Case {case_id: 'case-00000000000000aa', "
        "alert_id: $a, status: 'open', opened_at: '2026-11-18T10:00:00+00:00', "
        "opened_by: 'Dana Analyst'})-[:FOR_ALERT]->(al)",
        a=ALERT_ID,
        database_=DATABASE,
    )
    store = CaseStore(empty_graph, DATABASE, 10, {"High": 7, "Medium": 15, "Low": 30}, 30, (60, 35))
    assert store.migrate_legacy() == 1
    case = store.get("case-00000000000000aa")
    assert case["subject_customer_id"].startswith("cust-") and case["assigned_to"] == "Dana Analyst"
    assert case["due_at"] and case["sar_due_at"] and case["alerts"] >= 1
    assert [e["action"] for e in store.events("case-00000000000000aa")][0] == "migrated"
    assert store.migrate_legacy() == 0  # already migrated
