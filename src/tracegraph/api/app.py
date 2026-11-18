"""FastAPI service: health checks, `POST /ask` (cited answers), `POST /sar` (SAR drafts) and the
read-only workbench endpoints the UI uses (alerts, alert detail, subject profile, name screening).
The workbench endpoints run fixed Cypher templates through `run_read` and never call the LLM."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query
from neo4j import AsyncDriver, AsyncGraphDatabase, GraphDatabase
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient, QdrantClient

from tracegraph.config import Settings, settings
from tracegraph.er.embeddings import SentenceTransformerEmbedder
from tracegraph.generation.answer import (
    ALERT_NOTICE,
    ICIJ_NOTICE,
    SYNTHETIC_NOTICE,
    QAContext,
    answer_question,
)
from tracegraph.generation.sar_drafter import draft_sar
from tracegraph.graph.cypher_templates import (
    ALERT_SUMMARY,
    ALERTS,
    ALERTS_BY_TYPOLOGY,
    ALERTS_QUEUE,
    CUSTOMER_PROFILE,
    SAR_EVIDENCE,
    SCREEN_DETAILS,
    SUBJECT_TOTALS,
    TRANSACTIONS,
    WATCHLIST_LINKS,
    base_params,
)
from tracegraph.llm.factory import get_provider
from tracegraph.llm.metering import MeteredProvider
from tracegraph.retrieval.entity_linking import link_mention
from tracegraph.retrieval.graph_retriever import Evidence, run_read, to_evidence

logger = logging.getLogger(__name__)
if not logger.handlers:  # one usage line per LLM request on the server console (uvicorn style)
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

Typology = Literal["FAN-IN", "FAN-OUT", "CYCLE", "SCATTER-GATHER"]
ALERT_ID_PATTERN = r"^alert-[0-9a-f]{16}$"
SUBJECT_ID_PATTERN = r"^(cust-[0-9A-F]+|\d+:[0-9A-F]+)$"
SCREEN_LABELS = frozenset({"Customer", "SourceEntity"})
ACTIVITY_ROWS = 15  # per TRANSACTIONS part; three parts must fit under `qa_max_rows` (50)


def _log_usage(route: str, meter: MeteredProvider, started: float, status: str) -> dict[str, Any]:
    """Per-request LLM usage, logged as one line and returned in the response."""
    usage = {**meter.usage(), "seconds": round(time.monotonic() - started, 3)}
    logger.info("%s status=%s %s", route, status, " ".join(f"{k}={v}" for k, v in usage.items()))
    return usage


def _items(evidence: list[Evidence], *types: str) -> list[dict[str, Any]]:
    return [asdict(e) for e in evidence if e.type in types]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class SARRequest(BaseModel):
    alert_id: str = Field(pattern=ALERT_ID_PATTERN)


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
        meter = MeteredProvider(app.state.qa.provider)
        started = time.monotonic()
        response = asdict(answer_question(request.question, replace(app.state.qa, provider=meter)))
        response["llm_usage"] = _log_usage("/ask", meter, started, response["status"])
        return response

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

    def read(cypher: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        qa = app.state.qa
        return run_read(
            qa.driver, qa.database, cypher, {**base_params(min(limit, config.qa_max_rows)),
                                             **params}, config
        )  # fmt: skip

    @app.get("/alerts/summary")
    def alert_summary() -> dict[str, Any]:
        """Alert counts per typology (alerts, distinct flagged accounts, latest window end)."""
        rows = read(ALERT_SUMMARY, {}, config.qa_max_rows)
        return {"typologies": rows, "notices": [ALERT_NOTICE] if rows else []}

    @app.get("/alerts/queue")
    def alert_queue(
        typology: Typology | None = None,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Alerts newest first, optionally of one typology, one page at a time."""
        evidence = to_evidence(read(ALERTS_QUEUE, {"typology": typology, "offset": offset}, limit))
        return {
            "alerts": [asdict(e) for e in evidence],
            "offset": offset,
            "notices": [ALERT_NOTICE] if evidence else [],
        }

    @app.get("/alerts/{alert_id}")
    def alert_detail(alert_id: Annotated[str, Path(pattern=ALERT_ID_PATTERN)]) -> dict[str, Any]:
        """One alert with its flagged accounts (and owners) and its payments."""
        evidence = to_evidence(read(SAR_EVIDENCE, {"id": alert_id}, config.qa_max_rows))
        alert = next((asdict(e) for e in evidence if e.type == "Alert"), None)
        if alert is None:
            raise HTTPException(404, f"No alert {alert_id}.")
        return {
            "alert": alert,
            "accounts": _items(evidence, "Account"),
            "payments": _items(evidence, "SENT"),
            "notices": [ALERT_NOTICE, SYNTHETIC_NOTICE],
        }

    @app.get("/subjects/{subject_id}")
    def subject(
        subject_id: Annotated[str, Path(pattern=SUBJECT_ID_PATTERN, max_length=64)],
    ) -> dict[str, Any]:
        """A customer (by customer or account ID): profile, accounts, activity, largest
        payments, typology alerts and watchlist/offshore links."""
        profile = to_evidence(read(CUSTOMER_PROFILE, {"id": subject_id}, config.qa_max_rows))
        customer = next((asdict(e) for e in profile if e.type == "Customer"), None)
        if customer is None:
            raise HTTPException(404, f"No customer or account {subject_id}.")
        # Each section is capped so the three UNION parts fit under the row cap together.
        activity = to_evidence(read(TRANSACTIONS, {"id": customer["id"]}, ACTIVITY_ROWS))
        totals = {
            row["direction"]: {"payments": row["payments"], "currencies": row["currencies"]}
            for row in read(SUBJECT_TOTALS, {"id": customer["id"]}, 2)
        }
        alerts = to_evidence(read(ALERTS, {"id": customer["id"]}, 20))
        links = to_evidence(read(WATCHLIST_LINKS, {"id": customer["id"]}, 20))
        notices = [SYNTHETIC_NOTICE]
        if any(e.props.get("source") == "icij" for e in links if e.type == "SourceEntity"):
            notices.append(ICIJ_NOTICE)
        if alerts:
            notices.append(ALERT_NOTICE)
        return {
            "customer": customer,
            "accounts": _items(profile, "Account"),
            "totals": totals,
            "activity": _items(activity, "AccountActivity"),
            "activity_rows_per_direction": ACTIVITY_ROWS,
            "payments": _items(activity, "SENT"),
            "alerts": [asdict(e) for e in alerts],
            "links": _items(links, "SAME_AS", "POSSIBLE_SAME_AS"),
            "linked_entities": _items(links, "SourceEntity"),
            "notices": notices,
        }

    @app.get("/screen")
    def screen(
        name: Annotated[str, Query(min_length=2, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> dict[str, Any]:
        """Name screening against watchlist/offshore records and customers: the Q&A entity
        linker (full-text + vector), then each candidate's record."""
        qa = app.state.qa
        link = link_mention(
            name,
            qa.driver,
            qa.database,
            qa.qdrant,
            qa.embedder,
            config.model_copy(update={"qa_link_top_k": limit}),
            SCREEN_LABELS,
        )
        ids = [c.node_id for c in link.candidates]
        details = {e.id: e for e in to_evidence(read(SCREEN_DETAILS, {"ids": ids}, 2 * limit))}
        results = [
            {
                "id": c.node_id,
                "type": c.label,
                "name": c.name,
                "matched_by": list(c.via),
                "exact": link.chosen is not None and link.chosen.node_id == c.node_id,
                "props": details[c.node_id].props if c.node_id in details else {},
            }
            for c in link.candidates
        ]
        return {
            "query": name,
            "results": results,
            "notices": [SYNTHETIC_NOTICE, ICIJ_NOTICE] if results else [],
        }

    @app.post("/sar")
    def sar(request: SARRequest) -> dict[str, Any]:
        """A grounded SAR narrative for one alert, labeled 'DRAFT – requires analyst review'."""
        meter = MeteredProvider(app.state.qa.provider)
        started = time.monotonic()
        response = asdict(draft_sar(request.alert_id, replace(app.state.qa, provider=meter)))
        response["llm_usage"] = _log_usage("/sar", meter, started, response["status"])
        return response

    return app


app = create_app()
