"""The workbench pages render from API responses and validate input before calling the API."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")
ALERT = {
    "id": "alert-0123456789abcdef",
    "type": "Alert",
    "props": {"typology": "FAN-IN", "summary": "7 distinct senders paid account 1:A",
              "window_start": "2022-09-01T00:00", "window_end": "2022-09-02T00:00",
              "flagged_accounts": ["1:A"], "detection_thresholds": "min_senders=6"},
}  # fmt: skip
RESPONSES: dict[str, Any] = {
    "/alerts/summary": {
        "typologies": [
            {"typology": "FAN-IN", "alerts": 658, "accounts": 658, "latest": None},
            {"typology": "CYCLE", "alerts": 470, "accounts": 957, "latest": None},
        ],
        "notices": [],
    },  # fmt: skip
    "/alerts/queue": {"alerts": [ALERT], "offset": 0, "notices": []},
    "/alerts/alert-0123456789abcdef": {
        "alert": ALERT,
        "accounts": [
            {
                "id": "1:A",
                "type": "Account",
                "props": {"owner_customer_id": "cust-1A", "owner_name": "Acme"},
            }
        ],
        "payments": [
            {
                "id": "ibm-1",
                "type": "SENT",
                "props": {"amount_paid": 10.0, "payment_currency": "Euro"},
            }
        ],
        "notices": [],
    },  # fmt: skip
    "/subjects/cust-1A": {
        "customer": {
            "id": "cust-1A",
            "type": "Customer",
            "props": {
                "name": "Acme Ltd",
                "customer_type": "organization",
                "country": "GB",
                "synthetic": True,
            },
        },
        "accounts": [{"id": "1:A", "type": "Account", "props": {"bank_name": "Bank"}}],
        "totals": {
            "sent": {"payments": 76, "currencies": 1},
            "received": {"payments": 112, "currencies": 1},
        },
        "activity": [],
        "activity_rows_per_direction": 15,
        "payments": [],
        "alerts": [ALERT],
        "links": [],
        "linked_entities": [],
        "notices": [],
    },  # fmt: skip
    "/screen": {
        "query": "acme",
        "results": [
            {
                "id": "icij:7",
                "type": "SourceEntity",
                "name": "Acme Holdings",
                "matched_by": ["fulltext"],
                "exact": False,
                "props": {"source": "icij", "countries": ["vg"]},
            }
        ],
        "notices": [],
    },  # fmt: skip
    "/sar": {
        "alert_id": "alert-0123456789abcdef",
        "status": "drafted",
        "text": "DRAFT – requires analyst review\n\nThe alert ... [alert-0123456789abcdef]",
        "sentences": [{"text": "The alert ...", "citations": ["alert-0123456789abcdef"]}],
        "evidence": [{"id": "alert-0123456789abcdef", "type": "Alert", "props": {}}],
        "notices": [],
    },
}
RESPONSES["/alerts/summary"]["bands"] = {"High": 100, "Medium": 500, "Low": 528}
RESPONSES["/alerts/alert-0123456789abcdef"]["alert"] = {
    **ALERT,
    "props": {**ALERT["props"], "priority": 72, "priority_band": "High",
              "priority_breakdown": "scenario 15 + strength 12 + watchlist 30 + history 15"},
    "scenario": {"scenario_id": "TM-01", "name": "Funnel account activity",
                 "description": "Many accounts pay one account.", "red_flag": "Pooling.",
                 "next_steps": ["Check the senders."]},
}  # fmt: skip
CASE = {
    "case_id": "case-0123456789abcdef", "subject_customer_id": "cust-1A",
    "subject_name": "Acme Ltd", "status": "open", "priority": 72, "priority_band": "High",
    "assigned_to": "Dana Analyst", "opened_by": "Dana Analyst",
    "opened_at": "2026-11-18T10:00:00+00:00", "due_at": "2026-11-25T10:00:00+00:00",
    "sar_due_at": "2026-12-18T10:00:00+00:00", "updated_at": "2026-11-18T10:00:00+00:00",
    "sar_draft": None, "sar_reviewed_by": None, "alerts": 1,
}  # fmt: skip
RESPONSES["/cases"] = {"cases": [CASE], "offset": 0}
RESPONSES["/cases/case-0123456789abcdef"] = {
    "case": CASE,
    "case_alerts": [{"alert_id": "alert-0123456789abcdef", "typology": "FAN-IN",
                     "scenario": "TM-01 Funnel account activity", "priority": 72}],
    "selected_alert_id": "alert-0123456789abcdef",
    **{k: RESPONSES["/alerts/alert-0123456789abcdef"][k]
       for k in ("alert", "accounts", "payments", "notices")},
    "facts": {"payments": 1},
    "subgraph": {"nodes": [{"id": "1:A", "flagged": True, "owner": "Acme", "owner_id": "cust-1A"},
                           {"id": "2:B", "flagged": False, "owner": None, "owner_id": None}],
                 "edges": [{"id": "ibm-1", "source": "2:B", "target": "1:A", "amount": 10.0,
                            "currency": "Euro", "timestamp": "2022-09-01T10:00"}]},
    "audit": [{"seq": 1, "at": "2026-11-18T10:00:00+00:00", "analyst": "Dana Analyst",
               "role": "analyst", "action": "case_opened", "from_status": None,
               "to_status": "open", "rationale": None, "details": None}],
}  # fmt: skip
PENDING = {**CASE, "status": "pending_approval", "recommendation": "close",
           "recommended_by": "Sam Investigator", "recommended_role": "investigator",
           "recommendation_rationale": "No suspicious activity found."}  # fmt: skip


def _fake(method: str, path: str, **_: Any) -> dict[str, Any]:
    if method == "POST" and path == "/cases":
        return {"case": CASE, "created": True}
    return RESPONSES[path]


def _button(at: AppTest, label: str):  # type: ignore[no-untyped-def]
    return next(b for b in at.button if b.label == label)


def _app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=30)


def test_overview_shows_alert_volumes_from_the_api() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Open alerts"] == "1,128" and metrics["High priority"] == "100"
    assert {c.args[1] for c in request.call_args_list} == {"/alerts/summary", "/alerts/queue"}


def test_alert_queue_opens_the_selected_alert_for_review() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.session_state["queue_alert_id"] = "alert-0123456789abcdef"
        at.switch_page("views/alert_queue.py").run()
    assert not at.exception
    assert any("TM-01 Funnel account activity" in s.value for s in at.subheader)
    assert ("GET", "/alerts/alert-0123456789abcdef") in [c.args[:2] for c in
                                                         request.call_args_list]  # fmt: skip


def test_subject_profile_uses_uncapped_payment_totals() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake):
        at = _app().run()
        at.session_state["subject_query"] = "cust-1A"
        at.switch_page("views/subject.py").run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Payments sent"] == "76" and metrics["Payments received"] == "112"
    assert metrics["Typology alerts"] == "1"


def test_screening_lists_candidates() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.switch_page("views/screening.py").run()
        next(t for t in at.text_input if t.label == "Name to screen").set_value("acme")
        _button(at, "Screen").click().run()
    assert not at.exception
    assert request.call_args_list[-1].kwargs["params"]["name"] == "acme"
    assert at.dataframe[-1].value["Candidate"].tolist() == ["Acme Holdings"]


def test_malformed_alert_id_is_rejected_before_any_sar_call() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.switch_page("views/sar.py").run()
        calls_before = request.call_count
        at.text_input(key="sar_alert_id").set_value("MATCH (n) DELETE n")
        _button(at, "Draft SAR").click().run()
    assert not at.exception
    assert any("16 hex characters" in w.value for w in at.warning)
    assert request.call_count == calls_before


def test_a_drafted_sar_is_shown_with_its_label() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.switch_page("views/sar.py").run()
        at.text_input(key="sar_alert_id").set_value("alert-0123456789abcdef")
        _button(at, "Draft SAR").click().run()
    assert not at.exception
    assert ("POST", "/sar") in [c.args[:2] for c in request.call_args_list]
    assert any(c.value.startswith("DRAFT – requires analyst review") for c in at.code)


def test_case_view_draws_the_subgraph_and_lists_the_audit_trail() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake):
        at = _app().run()
        at.session_state["case_id"] = "case-0123456789abcdef"
        at.switch_page("views/cases.py").run()
    assert not at.exception
    dot = at.get("graphviz_chart")[0].proto.spec
    assert '"2:B" -> "1:A"' in dot and "Acme" in dot
    assert at.dataframe[-1].value["Action"].tolist() == ["Case opened"]


def test_open_case_needs_an_analyst_name_and_then_opens_the_case() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.session_state["queue_alert_id"] = "alert-0123456789abcdef"
        at.switch_page("views/alert_queue.py").run()
        _button(at, "Open case").click().run()
        assert any("analyst name" in w.value for w in at.warning)
        assert ("POST", "/cases") not in [c.args[:2] for c in request.call_args_list]
        at.session_state["analyst"] = "Dana Analyst"
        _button(at, "Open case").click().run()
    assert not at.exception
    opened = [c for c in request.call_args_list if c.args[:2] == ("POST", "/cases")]
    assert opened[0].kwargs["json"] == {"alert_id": "alert-0123456789abcdef",
                                         "analyst": "Dana Analyst", "role": "analyst"}  # fmt: skip


def test_a_recommendation_needs_confirmation_and_a_rationale_before_any_call() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.session_state["analyst"] = "Dana Analyst"
        at.session_state["case_id"] = "case-0123456789abcdef"
        at.switch_page("views/cases.py").run()
        next(t for t in at.text_area if t.label.startswith("Rationale")).set_value("No issue.")
        _button(at, "Submit for approval").click().run()
    assert not at.exception
    assert any("Confirm" in w.value for w in at.warning)
    assert not any(c.args[1].endswith("/recommendation") for c in request.call_args_list)


def _pending(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    if path == "/cases/case-0123456789abcdef":
        return {**RESPONSES[path], "case": PENDING}
    if path.endswith("/approval"):
        return {"case": {**PENDING, "status": "closed"}, "audit": []}
    return _fake(method, path, **kwargs)


def test_four_eyes_the_recommender_sees_no_approval_form() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_pending):
        at = _app().run()
        at.session_state["analyst"] = "Sam Investigator"
        at.session_state["role"] = "approver"
        at.session_state["case_id"] = "case-0123456789abcdef"
        at.switch_page("views/cases.py").run()
    assert not at.exception
    assert any("Four eyes" in w.value for w in at.warning)
    assert not any(b.label == "Record decision" for b in at.button)


def test_a_different_approver_can_approve_with_a_rationale() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_pending) as request:
        at = _app().run()
        at.session_state["analyst"] = "Alex Approver"
        at.session_state["role"] = "approver"
        at.session_state["case_id"] = "case-0123456789abcdef"
        at.switch_page("views/cases.py").run()
        next(t for t in at.text_area if t.label.startswith("Approver")).set_value(
            "Agree: the activity is explained by the customer's business."
        )
        at.checkbox[0].check()
        _button(at, "Record decision").click().run()
    assert not at.exception
    sent = [c for c in request.call_args_list if c.args[1].endswith("/approval")]
    assert sent and sent[0].kwargs["json"]["approve"] is True
    assert sent[0].kwargs["json"]["role"] == "approver"


def test_my_cases_lists_the_cases_assigned_to_me() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.session_state["analyst"] = "Dana Analyst"
        at.switch_page("views/cases.py").run()
    assert not at.exception
    listing = [c for c in request.call_args_list if c.args[:2] == ("GET", "/cases")]
    assert listing[0].kwargs["params"]["assigned_to"] == "Dana Analyst"
    assert at.dataframe[0].value["Customer"].tolist() == ["Acme Ltd"]


def test_replay_date_is_sent_with_the_overview_requests() -> None:
    from datetime import date

    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app()
        at.session_state["as_of"] = date(2022, 9, 5)
        at.run()
    assert not at.exception
    sent = [c.kwargs.get("params", {}).get("as_of") for c in request.call_args_list]
    assert sent == ["2022-09-05", "2022-09-05"]
    assert any("Replay" in i.value for i in at.info)
