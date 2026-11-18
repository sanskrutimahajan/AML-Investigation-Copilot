"""HTTP client for the API, used by the Streamlit UI (Phase 5).

The UI talks to the API only: it never opens a database session or calls the LLM itself, so
every guard (read-only Cypher, grounding check, DRAFT label) stays in one place.
"""

import re
from dataclasses import dataclass
from typing import Any

import httpx

ALERT_ID = re.compile(r"^alert-[0-9a-f]{16}$")
SUBJECT_ID = re.compile(r"^(cust-[0-9A-F]+|\d+:[0-9A-F]+)$")
TYPOLOGIES = ("FAN-IN", "FAN-OUT", "CYCLE", "SCATTER-GATHER")
STATUS_LABELS = {
    "answered": "Answered",
    "ambiguous": "Needs clarification",
    "not_found": "Not found",
    "insufficient_evidence": "Insufficient evidence",
    "drafted": "Drafted",
}


class APIError(Exception):
    """The API could not be reached or returned an error; the message is shown to the user."""


@dataclass
class APIClient:
    base_url: str
    timeout: float
    transport: httpx.BaseTransport | None = None  # tests inject a mock transport

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.timeout, transport=self.transport
            ) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise APIError(f"The API did not answer within {self.timeout:.0f} s.") from exc
        except httpx.HTTPError as exc:
            raise APIError(
                f"Could not reach the API at {self.base_url} ({exc}). Is `make api` running?"
            ) from exc
        if response.status_code >= 400:
            raise APIError(f"The API returned {response.status_code}: {_detail(response)}")
        body: dict[str, Any] = response.json()
        return body

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def ask(self, question: str) -> dict[str, Any]:
        return self._request("POST", "/ask", json={"question": question})

    def alerts(
        self, subject: str | None = None, typology: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if subject:
            params["subject"] = subject.strip()
        if typology:
            params["typology"] = typology
        return self._request("GET", "/alerts", params=params)

    def sar(self, alert_id: str) -> dict[str, Any]:
        return self._request("POST", "/sar", json={"alert_id": alert_id.strip()})

    def alert_summary(self) -> dict[str, Any]:
        return self._request("GET", "/alerts/summary")

    def alert_queue(
        self, typology: str | None = None, offset: int = 0, limit: int = 25
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if typology:
            params["typology"] = typology
        return self._request("GET", "/alerts/queue", params=params)

    def alert_detail(self, alert_id: str) -> dict[str, Any]:
        return self._request("GET", f"/alerts/{alert_id.strip()}")

    def subject(self, subject_id: str) -> dict[str, Any]:
        return self._request("GET", f"/subjects/{subject_id.strip()}")

    def screen(self, name: str, limit: int = 10) -> dict[str, Any]:
        return self._request("GET", "/screen", params={"name": name.strip(), "limit": limit})


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        return response.text[:300]
    return str(detail)[:300]


def evidence_rows(evidence: list[dict[str, Any]], cited: set[str]) -> list[dict[str, Any]]:
    """Evidence as table rows: cited items first, with their properties as one text column."""
    rows = [
        {
            "cited": item["id"] in cited,
            "id": item["id"],
            "type": item["type"],
            "details": "; ".join(
                f"{k}={_short(v)}" for k, v in sorted(item["props"].items()) if v is not None
            ),
        }
        for item in evidence
    ]
    return sorted(rows, key=lambda row: not row["cited"])


def cited_ids(sentences: list[dict[str, Any]]) -> set[str]:
    return {c for s in sentences for c in s.get("citations", [])}


def alert_rows(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`GET /alerts` items as table rows."""
    return [
        {
            "alert_id": a["id"],
            "typology": a["props"].get("typology"),
            "window_start": a["props"].get("window_start"),
            "window_end": a["props"].get("window_end"),
            "flagged_accounts": ", ".join(a["props"].get("flagged_accounts") or []),
            "summary": a["props"].get("summary"),
        }
        for a in alerts
    ]


def _short(value: Any, limit: int = 120) -> str:
    text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
