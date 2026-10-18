"""FastAPI service with dependency-aware health checks."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from neo4j import AsyncDriver, AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient

from tracegraph.config import Settings, settings

logger = logging.getLogger(__name__)


async def _check_neo4j(driver: AsyncDriver) -> None:
    await driver.verify_connectivity()


async def _check_qdrant(client: AsyncQdrantClient) -> None:
    await client.get_collections()


def create_app(config: Settings = settings) -> FastAPI:
    """Create the API application and initialize database clients at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        driver = AsyncGraphDatabase.driver(
            config.neo4j_uri,
            auth=(config.neo4j_username, config.neo4j_password),
        )
        client = AsyncQdrantClient(url=config.qdrant_url)
        app.state.neo4j_driver = driver
        app.state.qdrant_client = client
        try:
            yield
        finally:
            await driver.close()
            await client.close()

    app = FastAPI(title="AML Investigation Copilot API", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        checks: dict[str, Callable[[], Awaitable[None]]] = {
            "neo4j": lambda: _check_neo4j(app.state.neo4j_driver),
            "qdrant": lambda: _check_qdrant(app.state.qdrant_client),
        }
        outcomes = await asyncio.gather(
            *(check() for check in checks.values()),
            return_exceptions=True,
        )
        services: dict[str, str] = {}
        for name, outcome in zip(checks, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error("%s health check failed: %s", name, outcome)
                services[name] = "unhealthy"
            else:
                services[name] = "healthy"

        if all(status == "healthy" for status in services.values()):
            return {"status": "healthy", "services": services}
        raise HTTPException(
            status_code=503,
            detail={"status": "unhealthy", "services": services},
        )

    return app


app = create_app()
