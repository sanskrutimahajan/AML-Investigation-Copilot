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
