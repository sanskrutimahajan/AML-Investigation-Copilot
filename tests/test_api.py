"""API health endpoint tests."""

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
        TestClient(create_app(Settings())) as client,
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
        TestClient(create_app(Settings())) as client,
    ):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["detail"]["services"] == {
        "neo4j": "unhealthy",
        "qdrant": "healthy",
    }
