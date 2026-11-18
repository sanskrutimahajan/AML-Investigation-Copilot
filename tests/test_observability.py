"""Structured logs: request IDs on every line and response, and no names or IDs in the logs."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tracegraph.api.app import create_app
from tracegraph.config import Settings
from tracegraph.observability import JSONFormatter, RedactingFilter, redact


def test_redact_masks_ids_and_query_strings() -> None:
    text = "GET /subjects/cust-1A2B and 10:8000588D0, alert-0123456789abcdef /screen?name=Acme Ltd"
    assert (
        redact(text) == "GET /subjects/[redacted] and [redacted], [redacted] /screen[redacted] Ltd"
    )


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]


def test_access_log_uses_route_templates_and_a_request_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    link = MagicMock(candidates=[], chosen=None)
    with (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=AsyncMock()),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.SentenceTransformerEmbedder"),
        patch("tracegraph.api.app.link_mention", return_value=link),
        patch("tracegraph.api.app.run_read", return_value=[]),
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        screened = client.get("/screen", params={"name": "Sovona Benetti Limited"})
        traced = client.get("/subjects/cust-1A2B", headers={"X-Request-ID": "trace-12345678"})
        bad_id = client.get("/health", headers={"X-Request-ID": "x"})
    assert screened.headers["x-request-id"] and traced.headers["x-request-id"] == "trace-12345678"
    assert bad_id.headers["x-request-id"] != "x"  # malformed IDs are replaced

    lines = _lines(capsys)
    raw = json.dumps(lines)
    assert "Sovona" not in raw and "cust-1A2B" not in raw
    access = [line for line in lines if line["logger"] == "tracegraph.access"]
    assert {line["route"] for line in access} >= {"/screen", "/subjects/{subject_id}"}
    subject_line = next(line for line in access if line["route"] == "/subjects/{subject_id}")
    assert subject_line["request_id"] == "trace-12345678" and subject_line["status"] == 404


def test_json_formatter_keeps_only_known_extra_fields() -> None:
    record = logging.LogRecord(
        "tracegraph.x", logging.INFO, "f", 1, "case %s", ("case-0123456789abcdef",), None
    )
    record.status = 200
    record.secret = "do not log"
    RedactingFilter().filter(record)
    entry = json.loads(JSONFormatter().format(record))
    assert entry["msg"] == "case [redacted]" and entry["status"] == 200
    assert "secret" not in entry
