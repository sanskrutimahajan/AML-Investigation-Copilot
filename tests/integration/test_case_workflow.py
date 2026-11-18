"""One full case through the API against the throwaway Neo4j: alert → case → SAR draft → analyst
edit → disposition, with every action in the audit trail. The LLM is a stand-in that returns a
grounded draft, so the test checks the workflow, not model quality."""

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
CASE_ID = "case-0123456789abcdef"


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
    config = staged.model_copy(update={"neo4j_uri": staged.neo4j_test_uri,
                                       "neo4j_database": DATABASE})  # fmt: skip
    with (
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.SentenceTransformerEmbedder"),
        patch("tracegraph.api.app.get_provider", return_value=_GroundedLLM()),
        TestClient(create_app(config)) as test_client,
    ):
        yield test_client


def test_full_case_from_alert_to_filed_sar(client: TestClient) -> None:
    opened = client.post("/cases", json={"alert_id": ALERT_ID, "analyst": "Dana Analyst"})
    assert opened.status_code == 201 and opened.json()["case"]["status"] == "open"
    again = client.post("/cases", json={"alert_id": ALERT_ID, "analyst": "Someone Else"})
    assert again.status_code == 200 and again.json()["case"]["opened_by"] == "Dana Analyst"

    view = client.get(f"/cases/{CASE_ID}").json()
    assert view["alert"]["id"] == ALERT_ID
    assert any(n["flagged"] for n in view["subgraph"]["nodes"])
    assert len(view["subgraph"]["edges"]) == 1

    generated = client.post(f"/cases/{CASE_ID}/sar-draft", json={"analyst": "Dana Analyst"})
    assert generated.json()["draft"]["status"] == "drafted"
    case = generated.json()["case"]
    assert case["sar_draft"].startswith("DRAFT – requires analyst review")
    assert case["sar_reviewed_by"] is None

    # The copilot's draft alone cannot be filed: an analyst must review and save it.
    refused = client.post(f"/cases/{CASE_ID}/disposition", json={
        "analyst": "Dana Analyst", "decision": "file_sar",
        "rationale": "Pattern confirmed against the payments."})  # fmt: skip
    assert refused.status_code == 409 and "reviewed" in refused.json()["detail"]

    edited = client.put(f"/cases/{CASE_ID}/sar-draft", json={
        "analyst": "Dana Analyst", "text": "Edited narrative without the label."})  # fmt: skip
    assert edited.json()["case"]["sar_draft"].startswith("DRAFT – requires analyst review")
    assert edited.json()["case"]["sar_reviewed_by"] == "Dana Analyst"

    filed = client.post(f"/cases/{CASE_ID}/disposition", json={
        "analyst": "Dana Analyst", "decision": "file_sar",
        "rationale": "Pattern confirmed against the payments."})  # fmt: skip
    assert filed.status_code == 200 and filed.json()["case"]["status"] == "sar_filed"
    actions = [e["action"] for e in filed.json()["audit"]]
    assert actions == ["case_opened", "sar_draft_generated", "sar_draft_saved", "disposition"]
    assert filed.json()["audit"][-1]["rationale"] == "Pattern confirmed against the payments."

    # A final decision cannot be changed, and the draft is frozen.
    assert client.post(f"/cases/{CASE_ID}/disposition", json={
        "analyst": "Dana Analyst", "decision": "close",
        "rationale": "Trying to reopen a filed case."}).status_code == 409  # fmt: skip
    assert client.put(f"/cases/{CASE_ID}/sar-draft", json={
        "analyst": "Dana Analyst", "text": "Late edit."}).status_code == 409  # fmt: skip
    assert [c["case_id"] for c in client.get("/cases", params={"status": "sar_filed"})
            .json()["cases"]] == [CASE_ID]  # fmt: skip


def test_case_writes_stay_out_of_load_verification(empty_graph: Driver, staged: Settings,
                                                    client: TestClient) -> None:  # fmt: skip
    client.post("/cases", json={"alert_id": ALERT_ID, "analyst": "Dana Analyst"})
    # A repeat load with cases present still verifies (cases are analyst work, not staging data).
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    assert client.get("/cases/case-ffffffffffffffff").status_code == 404
    assert client.post("/cases", json={"alert_id": "alert-ffffffffffffffff",
                                       "analyst": "Dana Analyst"}).status_code == 404  # fmt: skip


def test_alerts_cannot_be_reset_while_cases_reference_them(empty_graph: Driver,
                                                           client: TestClient) -> None:  # fmt: skip
    client.post("/cases", json={"alert_id": ALERT_ID, "analyst": "Dana Analyst"})
    with pytest.raises(alerts.ExistingAlertsError, match="cases reference alerts"):
        alerts.reset_alerts(empty_graph, DATABASE)
    assert client.get(f"/cases/{CASE_ID}").json()["facts"]["payments"] == 1
