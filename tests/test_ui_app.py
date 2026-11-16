"""The Streamlit page renders without an API and validates input before calling it."""

from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "ui" / "app.py")


def test_page_renders_three_tabs_without_calling_the_api() -> None:
    with patch("tracegraph.api.client.APIClient._request") as request:
        at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Ask", "Alerts", "SAR draft"]
    assert not request.called


def test_malformed_alert_id_is_rejected_in_the_page() -> None:
    with patch("tracegraph.api.client.APIClient._request") as request:
        at = AppTest.from_file(APP, default_timeout=30).run()
        at.text_input(key="sar_alert_id").set_value("MATCH (n) DELETE n")
        at.tabs[2].button[0].click().run()
    assert not at.exception
    assert any("16 hex characters" in w.value for w in at.warning)
    assert not request.called


def test_a_drafted_sar_is_shown_with_its_label() -> None:
    draft = {
        "alert_id": "alert-0123456789abcdef",
        "status": "drafted",
        "text": "DRAFT – requires analyst review\n\nThe alert ... [alert-0123456789abcdef]",
        "sentences": [{"text": "The alert ...", "citations": ["alert-0123456789abcdef"]}],
        "evidence": [{"id": "alert-0123456789abcdef", "type": "Alert", "props": {}}],
        "notices": [],
    }
    with patch("tracegraph.api.client.APIClient._request", return_value=draft) as request:
        at = AppTest.from_file(APP, default_timeout=30).run()
        at.text_input(key="sar_alert_id").set_value("alert-0123456789abcdef")
        at.tabs[2].button[0].click().run()
    assert not at.exception
    assert request.call_args.args[:2] == ("POST", "/sar")
    assert any(c.value.startswith("DRAFT – requires analyst review") for c in at.code)
