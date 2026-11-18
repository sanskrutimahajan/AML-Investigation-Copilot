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


def _fake(method: str, path: str, **_: Any) -> dict[str, Any]:
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
