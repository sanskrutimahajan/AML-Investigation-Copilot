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
