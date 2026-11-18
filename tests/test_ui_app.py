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
CASE = {
    "case_id": "case-0123456789abcdef", "alert_id": "alert-0123456789abcdef", "status": "open",
    "opened_by": "Dana Analyst", "opened_at": "2026-11-18T10:00:00+00:00",
    "updated_at": "2026-11-18T10:00:00+00:00", "sar_draft": None, "sar_reviewed_by": None,
}  # fmt: skip
RESPONSES["/cases"] = {"cases": [CASE], "offset": 0}
RESPONSES["/cases/case-0123456789abcdef"] = {
    "case": CASE,
    **{k: RESPONSES["/alerts/alert-0123456789abcdef"][k]
       for k in ("alert", "accounts", "payments", "notices")},
    "subgraph": {"nodes": [{"id": "1:A", "flagged": True, "owner": "Acme", "owner_id": "cust-1A"},
                           {"id": "2:B", "flagged": False, "owner": None, "owner_id": None}],
                 "edges": [{"id": "ibm-1", "source": "2:B", "target": "1:A", "amount": 10.0,
                            "currency": "Euro", "timestamp": "2022-09-01T10:00"}]},
    "audit": [{"seq": 1, "at": "2026-11-18T10:00:00+00:00", "analyst": "Dana Analyst",
               "action": "case_opened", "from_status": None, "to_status": "open",
               "rationale": None, "details": None}],
}  # fmt: skip


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
    assert {m.label: m.value for m in at.metric}["Open alerts"] == "1,128"
    assert {c.args[1] for c in request.call_args_list} == {"/alerts/summary", "/alerts/queue"}


def test_alert_queue_opens_the_selected_alert_for_review() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.session_state["queue_alert_id"] = "alert-0123456789abcdef"
        at.switch_page("views/alert_queue.py").run()
    assert not at.exception
    assert any("Alert review" in s.value for s in at.subheader)
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
                                         "analyst": "Dana Analyst"}  # fmt: skip


def test_disposition_requires_confirmation_and_rationale_before_any_call() -> None:
    with patch("tracegraph.api.client.APIClient._request", side_effect=_fake) as request:
        at = _app().run()
        at.session_state["analyst"] = "Dana Analyst"
        at.session_state["case_id"] = "case-0123456789abcdef"
        at.switch_page("views/cases.py").run()
        next(t for t in at.text_area if t.label.startswith("Rationale")).set_value("No issue.")
        _button(at, "Record decision").click().run()
    assert not at.exception
    assert any("Confirm" in w.value for w in at.warning)
    assert not any(c.args[1].endswith("/disposition") for c in request.call_args_list)


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
