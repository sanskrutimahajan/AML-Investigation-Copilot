"""API health endpoint tests."""

import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tracegraph.api.app import create_app
from tracegraph.config import Settings


def test_health_reports_healthy_when_both_services_respond() -> None:
    driver = AsyncMock()
    qdrant_client = AsyncMock()

    with (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=driver),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=qdrant_client),
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "services": {"neo4j": "healthy", "qdrant": "healthy"},
    }
    driver.verify_connectivity.assert_awaited_once()
    qdrant_client.get_collections.assert_awaited_once()


def test_health_returns_503_when_a_service_is_unavailable() -> None:
    driver = AsyncMock()
    driver.verify_connectivity.side_effect = OSError("connection refused")
    qdrant_client = AsyncMock()

    with (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=driver),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=qdrant_client),
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["services"] == {
        "neo4j": "unhealthy",
        "qdrant": "healthy",
    }


def test_health_times_out_a_hanging_service() -> None:
    async def hang() -> None:
        await asyncio.sleep(10)

    driver = AsyncMock()
    qdrant_client = AsyncMock()
    qdrant_client.get_collections.side_effect = hang

    with (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=driver),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=qdrant_client),
        TestClient(
            create_app(Settings(_env_file=None, health_check_timeout_seconds=0.05))
        ) as client,
    ):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["services"]["qdrant"] == "unhealthy"


def test_ask_returns_the_answer_as_json_and_validates_input() -> None:
    from tracegraph.generation.answer import QAResponse
    from tracegraph.retrieval.graph_retriever import Evidence

    answer = QAResponse(
        "Who is cust-1?",
        "answered",
        "Anna is a customer. [cust-1]",
        sentences=[{"text": "Anna is a customer.", "citations": ["cust-1"]}],
        evidence=[Evidence("cust-1", "Customer", {"name": "Anna"})],
    )
    with (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=AsyncMock()),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.answer_question", return_value=answer) as ask,
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        response = client.post("/ask", json={"question": "Who is cust-1?"})
        empty = client.post("/ask", json={"question": ""})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["evidence"] == [{"id": "cust-1", "type": "Customer", "props": {"name": "Anna"}}]
    assert ask.call_args.args[0] == "Who is cust-1?"
    assert empty.status_code == 422
    # Each request gets its own metered provider; its usage is returned with the answer.
    from tracegraph.llm.metering import MeteredProvider

    assert isinstance(ask.call_args.args[1].provider, MeteredProvider)
    assert body["llm_usage"]["llm_calls"] == 0 and "seconds" in body["llm_usage"]


def test_sar_rejects_malformed_alert_ids() -> None:
    with (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=AsyncMock()),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.draft_sar") as draft,
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        bad = client.post("/sar", json={"alert_id": "MATCH (n) DETACH DELETE n"})
    assert bad.status_code == 422 and not draft.called


def _alerts_client(rows: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    return (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=AsyncMock()),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.run_read", return_value=rows),
    )


def test_alerts_by_subject_runs_the_read_only_template_and_adds_the_notice() -> None:
    from tracegraph.generation.answer import ALERT_NOTICE
    from tracegraph.graph.cypher_templates import ALERTS

    row = {"id": "alert-0123456789abcdef", "type": "Alert", "props": {"typology": "FAN-IN"}}
    driver_patch, qdrant_patch, read_patch = _alerts_client([row])
    with (
        driver_patch,
        qdrant_patch,
        read_patch as read,
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        response = client.get("/alerts", params={"subject": " 10:8000588D0 ", "limit": 5})

    assert response.status_code == 200
    assert response.json() == {
        "alerts": [{"id": "alert-0123456789abcdef", "type": "Alert",
                    "props": {"typology": "FAN-IN"}}],
        "notices": [ALERT_NOTICE],
    }  # fmt: skip
    cypher, params = read.call_args.args[2], read.call_args.args[3]
    assert cypher == ALERTS
    assert params["id"] == "10:8000588D0" and params["limit"] == 5


def test_alerts_by_typology_and_empty_results_have_no_notice() -> None:
    from tracegraph.graph.cypher_templates import ALERTS_BY_TYPOLOGY

    driver_patch, qdrant_patch, read_patch = _alerts_client([])
    with (
        driver_patch,
        qdrant_patch,
        read_patch as read,
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        response = client.get("/alerts", params={"typology": "CYCLE"})

    assert response.json() == {"alerts": [], "notices": []}
    assert read.call_args.args[2] == ALERTS_BY_TYPOLOGY
    assert read.call_args.args[3]["typology"] == "CYCLE"


def test_alerts_rejects_bad_filters_without_querying() -> None:
    driver_patch, qdrant_patch, read_patch = _alerts_client([])
    with (
        driver_patch,
        qdrant_patch,
        read_patch as read,
        TestClient(create_app(Settings(_env_file=None))) as client,
    ):
        neither = client.get("/alerts")
        both = client.get("/alerts", params={"subject": "x", "typology": "CYCLE"})
        unknown = client.get("/alerts", params={"typology": "MATCH (n) DELETE n"})
        too_many = client.get("/alerts", params={"typology": "CYCLE", "limit": 1000})

    assert [r.status_code for r in (neither, both, unknown, too_many)] == [422] * 4
    assert not read.called


def _client(read_side_effect):  # type: ignore[no-untyped-def]
    return (
        patch("tracegraph.api.app.AsyncGraphDatabase.driver", return_value=AsyncMock()),
        patch("tracegraph.api.app.AsyncQdrantClient", return_value=AsyncMock()),
        patch("tracegraph.api.app.run_read", side_effect=read_side_effect),
    )


def test_alert_queue_pages_newest_first_and_summary_counts_typologies() -> None:
    from tracegraph.graph.cypher_templates import ALERT_SUMMARY, ALERTS_QUEUE

    row = {"id": "alert-0123456789abcdef", "type": "Alert", "props": {"typology": "CYCLE"}}
    summary = [{"typology": "CYCLE", "alerts": 1, "accounts": 2, "latest": "2022-09-01T00:00"}]
    driver_patch, qdrant_patch, read_patch = _client(
        lambda _d, _db, cypher, _p, _c: summary if cypher == ALERT_SUMMARY else [row]
    )
    with driver_patch, qdrant_patch, read_patch as read, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        queue = client.get("/alerts/queue", params={"offset": 25, "limit": 25})
        typed = client.get("/alerts/queue", params={"typology": "CYCLE"})
        counts = client.get("/alerts/summary")
        bad = client.get("/alerts/queue", params={"typology": "x OR 1=1"})
    assert queue.json()["alerts"][0]["id"] == "alert-0123456789abcdef"
    first, second = read.call_args_list[0].args, read.call_args_list[1].args
    assert first[2] == ALERTS_QUEUE and first[3]["offset"] == 25 and first[3]["typology"] is None
    assert second[3]["typology"] == "CYCLE"
    assert typed.status_code == 200 and counts.json()["typologies"] == summary
    assert bad.status_code == 422 and read.call_count == 3
    assert first[3]["as_of"] is None


def test_replay_date_reaches_the_queue_and_summary_and_bad_dates_are_rejected() -> None:
    driver_patch, qdrant_patch, read_patch = _client(lambda *_: [])
    with driver_patch, qdrant_patch, read_patch as read, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        client.get("/alerts/queue", params={"as_of": "2022-09-05"})
        client.get("/alerts/summary", params={"as_of": "2022-09-05"})
        bad = client.get("/alerts/queue", params={"as_of": "2022-09-05' OR 1=1"})
    assert [c.args[3]["as_of"] for c in read.call_args_list] == ["2022-09-05", "2022-09-05"]
    assert bad.status_code == 422 and read.call_count == 2


def test_alert_detail_splits_evidence_and_404s_unknown_alerts() -> None:
    from tracegraph.graph.cypher_templates import SAR_EVIDENCE

    rows = [
        {"id": "alert-0123456789abcdef", "type": "Alert", "props": {"typology": "FAN-IN"}},
        {"id": "1:A", "type": "Account", "props": {"owner_customer_id": "cust-1"}},
        {"id": "ibm-1", "type": "SENT", "props": {"amount_paid": 5.0}},
    ]
    facts = [{"id": "alert-0123456789abcdef", "type": "AlertFacts", "props": {"payments": 1}}]
    responses = iter([rows, facts, []])
    driver_patch, qdrant_patch, read_patch = _client(lambda *_: next(responses))
    with driver_patch, qdrant_patch, read_patch as read, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        found = client.get("/alerts/alert-0123456789abcdef")
        missing = client.get("/alerts/alert-ffffffffffffffff")
        injected = client.get("/alerts/alert-0123 OR 1=1")
    body = found.json()
    assert body["alert"]["id"] == "alert-0123456789abcdef"
    assert [a["id"] for a in body["accounts"]] == ["1:A"]
    assert [p["id"] for p in body["payments"]] == ["ibm-1"]
    assert body["facts"] == {"payments": 1}
    assert read.call_args_list[0].args[2] == SAR_EVIDENCE
    assert missing.status_code == 404 and injected.status_code == 422 and read.call_count == 3


def test_subject_profile_combines_templates_and_adds_notices() -> None:
    from tracegraph.generation.answer import ALERT_NOTICE, ICIJ_NOTICE, SYNTHETIC_NOTICE
    from tracegraph.graph.cypher_templates import (
        ALERTS,
        CUSTOMER_PROFILE,
        SUBJECT_TOTALS,
        TRANSACTIONS,
        WATCHLIST_LINKS,
    )

    by_template = {
        CUSTOMER_PROFILE: [
            {"id": "cust-1A", "type": "Customer", "props": {"name": "X"}},
            {"id": "7:B", "type": "Account", "props": {"bank_name": "Bank"}},
        ],
        TRANSACTIONS: [
            {"id": "7:B", "type": "AccountActivity", "props": {"direction": "sent"}},
            {"id": "ibm-9", "type": "SENT", "props": {}},
        ],
        SUBJECT_TOTALS: [
            {"direction": "sent", "payments": 76, "currencies": 1},
            {"direction": "received", "payments": 112, "currencies": 2},
        ],
        ALERTS: [{"id": "alert-0123456789abcdef", "type": "Alert", "props": {}}],
        WATCHLIST_LINKS: [
            {"id": "cust-1A|SAME_AS|icij:1", "type": "SAME_AS", "props": {"probability": 1.0}},
            {"id": "icij:1", "type": "SourceEntity", "props": {"source": "icij"}},
        ],
    }
    driver_patch, qdrant_patch, read_patch = _client(lambda _d, _db, q, _p, _c: by_template[q])
    with driver_patch, qdrant_patch, read_patch as read, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        response = client.get("/subjects/7:B")
        injected = client.get("/subjects/x) DETACH DELETE (n")
    body = response.json()
    assert body["customer"]["id"] == "cust-1A"
    assert [len(body[k]) for k in ("accounts", "activity", "payments", "alerts", "links",
                                   "linked_entities")] == [1, 1, 1, 1, 1, 1]  # fmt: skip
    assert body["notices"] == [SYNTHETIC_NOTICE, ICIJ_NOTICE, ALERT_NOTICE]
    assert body["totals"] == {"sent": {"payments": 76, "currencies": 1},
                              "received": {"payments": 112, "currencies": 2}}  # fmt: skip
    # Every later section is asked about the customer, not the account that was looked up.
    assert all(c.args[3]["id"] == "cust-1A" for c in read.call_args_list[1:])
    assert injected.status_code in (404, 422) and read.call_count == 5


def test_screen_returns_linked_candidates_with_their_records() -> None:
    from tracegraph.retrieval.entity_linking import Candidate, Link

    hit = Candidate("icij:7", "SourceEntity", "Acme Ltd", ("fulltext", "vector"))
    link = Link("acme", [hit], hit)
    detail = [{"id": "icij:7", "type": "SourceEntity", "props": {"source": "icij"}}]
    driver_patch, qdrant_patch, read_patch = _client(lambda *_: detail)
    with driver_patch, qdrant_patch, read_patch, \
            patch("tracegraph.api.app.link_mention", return_value=link) as linker, \
            TestClient(create_app(Settings(_env_file=None))) as client:  # fmt: skip
        response = client.get("/screen", params={"name": "acme", "limit": 3})
        short = client.get("/screen", params={"name": "a"})
    result = response.json()["results"][0]
    assert result == {"id": "icij:7", "type": "SourceEntity", "name": "Acme Ltd",
                      "matched_by": ["fulltext", "vector"], "exact": True,
                      "props": {"source": "icij"}}  # fmt: skip
    assert linker.call_args.args[5].qa_link_top_k == 3
    assert linker.call_args.args[6] == frozenset({"Customer", "SourceEntity"})
    assert short.status_code == 422
