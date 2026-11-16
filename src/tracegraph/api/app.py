"""FastAPI service: health checks, `POST /ask` (cited answers), `GET /alerts` (alert lookup)
and `POST /sar` (SAR drafts)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from neo4j import AsyncDriver, AsyncGraphDatabase, GraphDatabase
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient, QdrantClient

from tracegraph.config import Settings, settings
from tracegraph.er.embeddings import SentenceTransformerEmbedder
from tracegraph.generation.answer import ALERT_NOTICE, QAContext, answer_question
from tracegraph.generation.sar_drafter import draft_sar
from tracegraph.graph.cypher_templates import ALERTS, ALERTS_BY_TYPOLOGY, base_params
from tracegraph.llm.factory import get_provider
from tracegraph.retrieval.graph_retriever import run_read, to_evidence

logger = logging.getLogger(__name__)

Typology = Literal["FAN-IN", "FAN-OUT", "CYCLE", "SCATTER-GATHER"]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class SARRequest(BaseModel):
    alert_id: str = Field(pattern=r"^alert-[0-9a-f]{16}$")


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
        # Q&A runs synchronously in FastAPI's thread pool; nothing connects before a question.
        qa_driver = GraphDatabase.driver(
            config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)
        )
        app.state.qa = QAContext(
            qa_driver,
            config.neo4j_database,
            QdrantClient(url=config.qdrant_url),
            SentenceTransformerEmbedder(config),
            get_provider(config),
            config,
        )
        try:
            yield
        finally:
            await driver.close()
            await client.close()
            qa_driver.close()

    app = FastAPI(title="AML Investigation Copilot API", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        checks: dict[str, Callable[[], Awaitable[None]]] = {
            "neo4j": lambda: _check_neo4j(app.state.neo4j_driver),
            "qdrant": lambda: _check_qdrant(app.state.qdrant_client),
        }
        timeout = config.health_check_timeout_seconds
        outcomes = await asyncio.gather(
            *(asyncio.wait_for(check(), timeout) for check in checks.values()),
            return_exceptions=True,
        )
        services: dict[str, str] = {}
        for name, outcome in zip(checks, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error("%s health check failed: %r", name, outcome)
                services[name] = "unhealthy"
            else:
                services[name] = "healthy"

        if all(status == "healthy" for status in services.values()):
            return {"status": "healthy", "services": services}
        raise HTTPException(
            status_code=503,
            detail={"status": "unhealthy", "services": services},
        )

    @app.post("/ask")
    def ask(request: AskRequest) -> dict[str, Any]:
        """Answer with sentences citing graph IDs, or a clarifying question, or insufficient
        evidence. The response includes the evidence, route, links, notices and a trace."""
        return asdict(answer_question(request.question, app.state.qa))

    @app.get("/alerts")
    def alerts(
        subject: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        typology: Typology | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Alerts flagging one account or a customer's accounts (`subject`), or alerts of one
        `typology`. Exactly one filter. Read-only template; no LLM involved."""
        if (subject is None) == (typology is None):
            raise HTTPException(422, "Give exactly one of 'subject' or 'typology'.")
        qa = app.state.qa
        params: dict[str, str]
        if subject is not None:
            cypher, params = ALERTS, {"id": subject.strip()}
        else:
            cypher, params = ALERTS_BY_TYPOLOGY, {"typology": str(typology)}
        rows = run_read(
            qa.driver,
            qa.database,
            cypher,
            {**base_params(min(limit, config.qa_max_rows)), **params},
            config,
        )
        evidence = to_evidence(rows)
        return {
            "alerts": [asdict(e) for e in evidence],
            "notices": [ALERT_NOTICE] if evidence else [],
        }

    @app.post("/sar")
    def sar(request: SARRequest) -> dict[str, Any]:
        """A grounded SAR narrative for one alert, labeled 'DRAFT – requires analyst review'."""
        return asdict(draft_sar(request.alert_id, app.state.qa))

    return app


app = create_app()
