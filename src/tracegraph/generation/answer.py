"""Cited answers: route -> link -> retrieve -> draft -> grounding check (Phase 3).

- Template intents link their mentions (only to the node labels the template accepts) and run the
  template; `other`, or a template question with no mention, goes to guarded text-to-Cypher.
- A payment path is a structured result, so its answer is written by code (one sentence per
  payment, each citing its transaction), not by the LLM; it still passes the grounding check.
- An ambiguous or unknown mention is returned as a question back to the user; nothing is guessed.
- The LLM drafts sentences, each citing evidence IDs. A draft that fails the grounding check is
  regenerated once with the problems listed; if it fails again, the answer is "insufficient
  evidence".
- Notices about synthetic links and ICIJ data are fixed text added by code, never by the LLM.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from neo4j import Driver
from neo4j.exceptions import Neo4jError
from pydantic import BaseModel
from qdrant_client import QdrantClient

from tracegraph.config import Settings
from tracegraph.er.embeddings import Embedder
from tracegraph.generation.grounding_check import clean_citation, grounding_problems
from tracegraph.graph.cypher_templates import TEMPLATES, base_params, cypher_for
from tracegraph.llm.base import LLMProvider
from tracegraph.llm.structured import StructuredOutputError, generate_json
from tracegraph.retrieval.entity_linking import Link, link_mention
from tracegraph.retrieval.fusion import select_evidence
from tracegraph.retrieval.graph_retriever import Evidence, run_read, to_evidence
from tracegraph.retrieval.router import Route, route
from tracegraph.retrieval.text2cypher import answer_with_cypher

INSUFFICIENT = "Insufficient evidence: the retrieved graph data does not support an answer."
SYNTHETIC_NOTICE = (
    "Customer records are synthetic, and so are their links to watchlist or offshore records: "
    "a link is not evidence about the real person or company."
)
ICIJ_NOTICE = "Appearing in the ICIJ Offshore Leaks database does not by itself imply wrongdoing."
_ER_EDGES = "MATCH ()-[r:SAME_AS|POSSIBLE_SAME_AS]->() RETURN count(r) AS n"

SYSTEM = """You answer an anti-money-laundering investigator's question using ONLY the evidence \
lines below. Each line starts with its ID in brackets.

- Write short factual sentences. Each sentence lists, in `citations`, the IDs of the evidence \
lines it relies on.
- Copy numbers exactly as they appear in the evidence (rounding is fine). Never compute new \
totals, and never add amounts in different currencies.
- If the evidence does not answer the question, say what it does show.
- Never speculate about guilt or wrongdoing.
Reply with JSON only."""


class Sentence(BaseModel):
    text: str
    citations: list[str]


class Draft(BaseModel):
    sentences: list[Sentence]


Status = Literal["answered", "insufficient_evidence", "ambiguous", "not_found"]


@dataclass
class QAContext:
    driver: Driver
    database: str
    qdrant: QdrantClient
    embedder: Embedder
    provider: LLMProvider
    config: Settings


@dataclass
class QAResponse:
    question: str
    status: Status
    answer: str
    sentences: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)


def notices_for(evidence: list[Evidence]) -> list[str]:
    notices = []
    if any(e.type in {"Customer", "SAME_AS", "POSSIBLE_SAME_AS"} for e in evidence):
        notices.append(SYNTHETIC_NOTICE)
    if any(e.props.get("source") == "icij" or e.props.get("source_leak") for e in evidence):
        notices.append(ICIJ_NOTICE)
    return notices


def _link_dict(link: Link) -> dict[str, Any]:
    return {
        "mention": link.mention,
        "chosen": link.chosen and vars(link.chosen),
        "candidates": [vars(c) for c in link.candidates],
    }


def retrieve(
    question: str, plan: Route, ctx: QAContext, trace: dict[str, Any]
) -> tuple[list[Evidence], list[Link]]:
    """Evidence for a routed question, plus the links made on the way."""
    config = ctx.config
    template = TEMPLATES.get(plan.intent)
    needed = 2 if plan.intent == "money_path" else 1
    if template is None or len(plan.mentions) < needed:
        evidence, cypher_trace = answer_with_cypher(
            question, ctx.provider, ctx.driver, ctx.database, config
        )
        trace["text2cypher"] = cypher_trace
        return evidence, []
    links = [
        link_mention(
            mention, ctx.driver, ctx.database, ctx.qdrant, ctx.embedder, config,
            template.subject_labels,
        )
        for mention in plan.mentions[:needed]
    ]  # fmt: skip
    if any(link.chosen is None for link in links):
        return [], links
    ids = [link.chosen.node_id for link in links if link.chosen]
    params = {
        **base_params(config.qa_max_rows),
        "id": ids[0],
        "start": plan.start_date and plan.start_date.isoformat(),
        "end": plan.end_date and plan.end_date.isoformat(),
    }
    cypher = cypher_for(plan.intent, config.qa_max_hops)
    if plan.intent != "money_path":
        return to_evidence(run_read(ctx.driver, ctx.database, cypher, params, config)), links
    # Either direction counts as a connection: first -> second, else second -> first.
    for sender, receiver in ((ids[0], ids[1]), (ids[1], ids[0])):
        rows = run_read(
            ctx.driver, ctx.database, cypher, {**params, "id": sender, "other": receiver}, config
        )
        if rows:
            trace["path"] = {"from": sender, "to": receiver}
            return to_evidence(rows), links
    return [], links


def path_sentences(
    evidence: list[Evidence], ends: tuple[str, str], max_hops: int
) -> list[Sentence]:
    """The money-path answer, written from the template result (no LLM).

    The shortest path ignores time, so the answer claims a connection; it says the chain is
    consistent with a flow of funds only when the payments are in time order.
    """
    hops = sorted(evidence, key=lambda e: e.props["step"])
    if not hops:
        text = (
            f"No chain of up to {max_hops} payments was found between {ends[0]} and {ends[1]}, "
            "in either direction."
        )
        return [Sentence(text=text, citations=list(ends))]
    first, last = hops[0].props, hops[-1].props
    times = [h.props["timestamp"] for h in hops]
    order = (
        f"The payments are in time order ({times[0]} to {times[-1]}), consistent with a flow of "
        "funds."
        if times == sorted(times)
        else "The payments are not in time order, so the chain shows a connection, not a traced "
        "flow of the same funds."
    )
    sentences = [
        Sentence(
            text=f"{ends[0]} (account {first['from_account']}) is connected to {ends[1]} "
            f"(account {last['to_account']}) through a chain of {len(hops)} payments. {order}",
            citations=[h.id for h in hops],
        )
    ]
    for hop in hops:
        p = hop.props
        sentences.append(
            Sentence(
                text=f"Payment {p['step']}: {hop.id} on {p['timestamp']}, {p['amount_paid']:.2f} "
                f"{p['payment_currency']} ({p['payment_format']}) from {p['from_account']} to "
                f"{p['to_account']}.",
                citations=[hop.id],
            )
        )
    return sentences


def draft_answer(
    question: str, evidence: list[Evidence], ctx: QAContext, trace: dict[str, Any]
) -> list[Sentence] | None:
    """A grounded draft, or None after one failed regeneration (or invalid JSON)."""
    by_id: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_id.setdefault(item.id, []).append(item)
    context = "\n".join(item.render() for item in evidence)
    prompt = f"Question: {question}\n\nEvidence:\n{context}"
    trace["drafts"] = []
    for _ in range(2):
        try:
            draft = generate_json(
                ctx.provider, prompt, Draft, system=SYSTEM, retries=ctx.config.llm_json_retries
            ).value
        except StructuredOutputError as exc:
            trace["drafts"].append({"error": str(exc)[:300]})
            return None
        for sentence in draft.sentences:
            sentence.citations = [clean_citation(c) for c in sentence.citations]
        problems = grounding_problems(draft.sentences, by_id, question)
        trace["drafts"].append({"sentences": draft.model_dump()["sentences"], "problems": problems})
        if not problems:
            return draft.sentences
        prompt = (
            f"Question: {question}\n\nEvidence:\n{context}\n\nYour previous answer failed the "
            "grounding check:\n- " + "\n- ".join(problems) + "\nWrite a corrected answer."
        )
    return None


def _answered(response: QAResponse, sentences: list[Sentence], evidence: list[Evidence]) -> None:
    response.status = "answered"
    response.evidence = evidence
    response.sentences = [s.model_dump() for s in sentences]
    response.answer = " ".join(f"{s.text} [{', '.join(s.citations)}]" for s in sentences)
    response.notices = notices_for(evidence)


def answer_question(question: str, ctx: QAContext) -> QAResponse:
    started = time.monotonic()
    trace: dict[str, Any] = {}
    try:
        plan = route(question, ctx.provider, ctx.config).value
        evidence, links = retrieve(question, plan, ctx, trace)
    except (StructuredOutputError, Neo4jError) as exc:  # invalid LLM JSON, query timeout, ...
        trace["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        trace["seconds"] = round(time.monotonic() - started, 2)
        return QAResponse(question, "insufficient_evidence", INSUFFICIENT, trace=trace)
    response = QAResponse(
        question, "insufficient_evidence", INSUFFICIENT,
        route=plan.model_dump(mode="json"), links=[_link_dict(link) for link in links], trace=trace,
    )  # fmt: skip
    unresolved = next((link for link in links if link.chosen is None), None)
    if unresolved and unresolved.ambiguous:
        options = "; ".join(f"{c.name} ({c.label}, {c.node_id})" for c in unresolved.candidates)
        response.status = "ambiguous"
        response.answer = f"Which '{unresolved.mention}' do you mean? Candidates: {options}."
    elif unresolved:
        response.status = "not_found"
        response.answer = (
            f"No customer, account or entity matching '{unresolved.mention}' was found."
        )
    elif plan.intent == "money_path":
        linked = [link.chosen.node_id for link in links if link.chosen]
        direction = trace.get("path", {"from": linked[0], "to": linked[1]})
        path = path_sentences(
            evidence, (direction["from"], direction["to"]), ctx.config.qa_max_hops
        )
        if not evidence:  # a negative result cites the two linked nodes and the search made
            evidence = [
                Evidence(
                    link.chosen.node_id,
                    link.chosen.label,
                    {"name": link.chosen.name, "payments_searched_up_to": ctx.config.qa_max_hops},
                )
                for link in links
                if link.chosen
            ]
        if not grounding_problems(path, {item.id: [item] for item in evidence}, question):
            _answered(response, path, evidence)
    elif not evidence:
        if (
            plan.intent == "watchlist_links"
            and not run_read(ctx.driver, ctx.database, _ER_EDGES, {}, ctx.config)[0]["n"]
        ):
            response.answer = (
                "Entity resolution has not written any watchlist links to the graph yet, "
                "so watchlist matches cannot be checked."
            )
    else:
        evidence = select_evidence(evidence, ctx.config.qa_context_max_items)
        response.evidence = evidence
        sentences = draft_answer(question, evidence, ctx, trace)
        if sentences:
            _answered(response, sentences, evidence)
    trace["evidence_items"] = len(evidence)
    trace["seconds"] = round(time.monotonic() - started, 2)
    return response
