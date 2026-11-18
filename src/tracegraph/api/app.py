"""FastAPI service: health checks, `POST /ask` (cited answers), `POST /sar` (SAR drafts), the
read-only workbench endpoints the UI uses (alerts, alert detail, subject profile, name screening),
and the case endpoints (`/cases*`): open a case for an alert, draft and edit its SAR, and record the
analyst's disposition. Workbench reads run fixed Cypher templates through `run_read` and never call
the LLM. Case writes go only through `tracegraph.cases.store`, and each one is audited."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query, Response
from neo4j import AsyncDriver, AsyncGraphDatabase, GraphDatabase
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import AsyncQdrantClient, QdrantClient

from tracegraph.cases.store import CaseError, CaseStore, Decision, subgraph
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
    ALERT_FACTS,
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
from tracegraph.observability import RequestContextMiddleware, configure_logging
from tracegraph.retrieval.entity_linking import link_mention
from tracegraph.retrieval.graph_retriever import Evidence, run_read, to_evidence

logger = logging.getLogger(__name__)

Typology = Literal[
    "FAN-IN", "FAN-OUT", "CYCLE", "SCATTER-GATHER", "LAYERING", "MULE-HUB", "STRUCTURING"
]
ALERT_ID_PATTERN = r"^alert-[0-9a-f]{16}$"
SUBJECT_ID_PATTERN = r"^(cust-[0-9A-F]+|\d+:[0-9A-F]+)$"
CASE_ID_PATTERN = r"^case-[0-9a-f]{16}$"
# Self-declared; there is no authentication in this prototype (AUDIT.md, decision 8).
ANALYST_PATTERN = r"^[A-Za-z][A-Za-z .'-]{1,63}$"
CaseStatus = Literal["open", "escalated", "closed", "sar_filed"]
SCREEN_LABELS = frozenset({"Customer", "SourceEntity"})
ACTIVITY_ROWS = 15  # per TRANSACTIONS part; three parts must fit under `qa_max_rows` (50)


def _log_usage(route: str, meter: MeteredProvider, started: float, status: str) -> dict[str, Any]:
    """Per-request LLM usage, logged as one line and returned in the response."""
    usage = {**meter.usage(), "seconds": round(time.monotonic() - started, 3)}
    logger.info(
        "%s status=%s %s", route, status, " ".join(f"{k}={v}" for k, v in usage.items()),
        extra={"route": route, "outcome": status, **{k: usage[k] for k in
               ("llm_calls", "input_tokens", "output_tokens", "llm_seconds")}},
    )  # fmt: skip
    return usage


def _items(evidence: list[Evidence], *types: str) -> list[dict[str, Any]]:
    return [asdict(e) for e in evidence if e.type in types]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class SARRequest(BaseModel):
    alert_id: str = Field(pattern=ALERT_ID_PATTERN)


class OpenCaseRequest(BaseModel):
    alert_id: str = Field(pattern=ALERT_ID_PATTERN)
    analyst: str = Field(pattern=ANALYST_PATTERN)


class AnalystRequest(BaseModel):
    analyst: str = Field(pattern=ANALYST_PATTERN)


class SaveDraftRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    analyst: str = Field(pattern=ANALYST_PATTERN)
    text: str = Field(min_length=1, max_length=20_000)


class DispositionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    analyst: str = Field(pattern=ANALYST_PATTERN)
    decision: Decision
    rationale: str = Field(min_length=10, max_length=2_000)


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
        app.state.cases = CaseStore(qa_driver, config.neo4j_database,
                                    config.qa_query_timeout_seconds)  # fmt: skip
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

    configure_logging(config.log_format, config.log_level)
    app = FastAPI(title="AML Investigation Copilot API", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)

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
    def alert_summary(as_of: date | None = None) -> dict[str, Any]:
        """Alert counts per typology (alerts, distinct flagged accounts, latest window end).
        `as_of` replays the state on that day: only alerts whose window had ended."""
        rows = read(ALERT_SUMMARY, {"as_of": as_of and as_of.isoformat()}, config.qa_max_rows)
        return {"typologies": rows, "notices": [ALERT_NOTICE] if rows else []}

    @app.get("/alerts/queue")
    def alert_queue(
        typology: Typology | None = None,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Alerts newest first, optionally of one typology and as of one day, a page at a time."""
        params = {"typology": typology, "offset": offset, "as_of": as_of and as_of.isoformat()}
        evidence = to_evidence(read(ALERTS_QUEUE, params, limit))
        return {
            "alerts": [asdict(e) for e in evidence],
            "offset": offset,
            "notices": [ALERT_NOTICE] if evidence else [],
        }

    def alert_evidence(alert_id: str) -> dict[str, Any]:
        evidence = to_evidence(read(SAR_EVIDENCE, {"id": alert_id}, config.qa_max_rows))
        alert = next((asdict(e) for e in evidence if e.type == "Alert"), None)
        if alert is None:
            raise HTTPException(404, f"No alert {alert_id}.")
        # The alert's payments re-read from the graph and summarized: a check on the detector.
        facts = to_evidence(read(ALERT_FACTS, {"id": alert_id}, 1))
        return {
            "alert": alert,
            "accounts": _items(evidence, "Account"),
            "payments": _items(evidence, "SENT"),
            "facts": next((e.props for e in facts if e.type == "AlertFacts"), {}),
            "notices": [ALERT_NOTICE, SYNTHETIC_NOTICE],
        }

    @app.get("/alerts/{alert_id}")
    def alert_detail(alert_id: Annotated[str, Path(pattern=ALERT_ID_PATTERN)]) -> dict[str, Any]:
        """One alert with its flagged accounts (and owners) and its payments."""
        return alert_evidence(alert_id)

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

    # --- Cases: the analyst's workflow. The copilot drafts; only the analyst disposes. ---------

    def cases() -> CaseStore:
        store: CaseStore = app.state.cases
        return store

    def case_call(work: Callable[[], Any]) -> Any:
        try:
            return work()
        except CaseError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

    CaseId = Annotated[str, Path(pattern=CASE_ID_PATTERN)]

    @app.post("/cases")
    def open_case(request: OpenCaseRequest, response: Response) -> dict[str, Any]:
        """Open the case for an alert (one case per alert; an existing case is returned)."""
        case, created = case_call(lambda: cases().open_case(request.alert_id, request.analyst))
        response.status_code = 201 if created else 200
        return {"case": case, "created": created}

    @app.get("/cases")
    def list_cases(
        status: CaseStatus | None = None,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Cases, most recently updated first, optionally of one status."""
        return {"cases": cases().list(status, offset, limit), "offset": offset}

    @app.get("/cases/{case_id}")
    def get_case(case_id: CaseId) -> dict[str, Any]:
        """The case, its alert evidence, a drawable subgraph and the audit trail."""
        case = case_call(lambda: cases().get(case_id))
        evidence = alert_evidence(case["alert_id"])
        return {
            "case": case,
            **evidence,
            "subgraph": subgraph(evidence["accounts"], evidence["payments"]),
            "audit": cases().events(case_id),
        }

    @app.post("/cases/{case_id}/sar-draft")
    def generate_case_draft(case_id: CaseId, request: AnalystRequest) -> dict[str, Any]:
        """Draft the SAR narrative with the LLM and store it as an unreviewed draft. An
        abstention ('insufficient evidence') is returned and nothing is stored."""
        case = case_call(lambda: cases().get(case_id))
        meter = MeteredProvider(app.state.qa.provider)
        started = time.monotonic()
        draft = asdict(draft_sar(case["alert_id"], replace(app.state.qa, provider=meter)))
        draft["llm_usage"] = _log_usage("/cases/sar-draft", meter, started, draft["status"])
        if draft["status"] == "drafted":
            case = case_call(
                lambda: cases().save_draft(case_id, draft["text"], request.analyst, "generated")
            )
        return {"case": case, "draft": draft}

    @app.put("/cases/{case_id}/sar-draft")
    def save_case_draft(case_id: CaseId, request: SaveDraftRequest) -> dict[str, Any]:
        """Save the analyst's edited (reviewed) draft. The DRAFT label is kept."""
        case = case_call(
            lambda: cases().save_draft(case_id, request.text, request.analyst, "analyst")
        )
        return {"case": case}

    @app.post("/cases/{case_id}/disposition")
    def dispose_case(case_id: CaseId, request: DispositionRequest) -> dict[str, Any]:
        """Record the analyst's decision: close, escalate, or file SAR (needs a reviewed draft).
        Nothing is sent anywhere; the decision and its rationale are audited."""
        case = case_call(
            lambda: cases().dispose(
                case_id, request.decision, request.analyst, request.rationale.strip()
            )  # fmt: skip
        )
        return {"case": case, "audit": cases().events(case_id)}

    return app


app = create_app()
