"""The UI's API client and table helpers (Phase 5); no network, a mock transport."""

import json

import httpx
import pytest

from tracegraph.api.client import APIClient, APIError, alert_rows, cited_ids, evidence_rows


def _client(handler) -> APIClient:  # type: ignore[no-untyped-def]
    return APIClient("http://api.test", 5.0, transport=httpx.MockTransport(handler))


def test_requests_hit_the_right_endpoints_with_the_right_payloads() -> None:
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content.decode() or
                     str(request.url.params)))  # fmt: skip
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.ask("Who owns account 1?")
    client.alerts(subject=" 10:8000588D0 ", limit=5)
    client.alerts(typology="CYCLE")
    client.sar(" alert-0123456789abcdef ")
    client.health()

    assert seen[0][:2] == ("POST", "/ask")
    assert json.loads(seen[0][2]) == {"question": "Who owns account 1?"}
    assert seen[1] == ("GET", "/alerts", "limit=5&subject=10%3A8000588D0")
    assert seen[2] == ("GET", "/alerts", "limit=20&typology=CYCLE")
    assert json.loads(seen[3][2]) == {"alert_id": "alert-0123456789abcdef"}
    assert seen[4][:2] == ("GET", "/health")


def test_http_errors_and_unreachable_api_become_readable_api_errors() -> None:
    failing = _client(lambda r: httpx.Response(422, json={"detail": "Give exactly one"}))
    with pytest.raises(APIError, match="422: Give exactly one"):
        failing.alerts()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(APIError, match="make api"):
        _client(refuse).health()

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(APIError, match="within 5 s"):
        _client(slow).sar("alert-0123456789abcdef")


def test_evidence_rows_put_cited_items_first_and_alert_rows_flatten_props() -> None:
    evidence = [
        {"id": "acct-1", "type": "Account", "props": {"bank_id": "b1", "note": None}},
        {"id": "cust-1", "type": "Customer", "props": {"name": "Anna", "tags": ["a", "b"]}},
    ]
    sentences = [{"text": "Anna is a customer.", "citations": ["cust-1"]}]
    rows = evidence_rows(evidence, cited_ids(sentences))
    assert [(r["id"], r["cited"]) for r in rows] == [("cust-1", True), ("acct-1", False)]
    assert rows[0]["details"] == "name=Anna; tags=a, b"
    assert rows[1]["details"] == "bank_id=b1"  # nulls left out

    alerts = [
        {
            "id": "alert-1",
            "type": "Alert",
            "props": {"typology": "CYCLE", "flagged_accounts": ["a1", "a2"], "summary": "s"},
        }
    ]
    assert alert_rows(alerts)[0] == {
        "alert_id": "alert-1", "typology": "CYCLE", "window_start": None, "window_end": None,
        "flagged_accounts": "a1, a2", "summary": "s",
    }  # fmt: skip


def test_workbench_requests_hit_their_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, str(request.url.params)))
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    client.alert_summary()
    client.alert_queue()
    client.alert_queue(typology="FAN-OUT", offset=50, limit=25)
    client.alert_detail(" alert-0123456789abcdef ")
    client.subject(" 10:8000588D0 ")
    client.screen(" Acme Holdings ", limit=5)

    assert seen == [
        ("/alerts/summary", ""),
        ("/alerts/queue", "offset=0&limit=25"),
        ("/alerts/queue", "offset=50&limit=25&typology=FAN-OUT"),
        ("/alerts/alert-0123456789abcdef", ""),
        ("/subjects/10:8000588D0", ""),
        ("/screen", "name=Acme+Holdings&limit=5"),
    ]
