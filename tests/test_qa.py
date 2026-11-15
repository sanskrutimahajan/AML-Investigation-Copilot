"""Phase 3 logic without services: routing, fusion, path answers, notices, drafting, flow."""

from datetime import date
from typing import Any
from unittest.mock import patch

from test_llm import ScriptedProvider

from tracegraph.config import Settings
from tracegraph.generation import answer as qa
from tracegraph.retrieval import router
from tracegraph.retrieval.entity_linking import Candidate, Link, lucene_query
from tracegraph.retrieval.fusion import rrf, select_evidence
from tracegraph.retrieval.graph_retriever import Evidence

CONFIG = Settings(_env_file=None)


def _ctx(provider: Any) -> qa.QAContext:
    return qa.QAContext(None, "neo4j", None, None, provider, CONFIG)  # type: ignore[arg-type]


def _route(intent: str, mentions: list[str]) -> str:
    return (
        f'{{"intent": "{intent}", "mentions": {mentions}, "start_date": null, "end_date": null}}'
    ).replace("'", '"')


def test_rrf_rewards_agreement_and_select_evidence_dedupes() -> None:
    assert [node for node, _ in rrf([["a", "b"], ["b", "c"]], 60)] == ["b", "a", "c"]
    item = Evidence("x", "Customer", {"name": "Anna"})
    assert select_evidence([item, item, Evidence("y", "Customer", {})], 5) == [
        item,
        Evidence("y", "Customer", {}),
    ]
    assert len(select_evidence([Evidence(str(i), "T", {}) for i in range(9)], 3)) == 3


def test_lucene_query_only_passes_safe_words() -> None:
    assert (
        lucene_query('Anna "OR" Schmidt) AND *') == "anna~1 or schmidt~1 and"
    )  # lower case: not Lucene operators
    assert lucene_query("!!!") == ""


def test_router_drops_mentions_that_are_not_in_the_question() -> None:
    provider = ScriptedProvider(
        [_route("source_network", ["opensanctions:Q1"]), _route("source_network", ["Q2"])]
    )
    result = router.route("Who are the officers of HOTFOCUS CO LTD?", provider, CONFIG)
    assert result.value.mentions == []  # invented twice, so dropped
    assert "not in the question" in provider.prompts[1]
    kept = ScriptedProvider([_route("source_network", ["hotfocus co., ltd."])])
    assert router.route("Officers of HOTFOCUS CO LTD?", kept, CONFIG).value.mentions == [
        "hotfocus co., ltd."
    ]


def _hop(step: int, tx: str, when: str) -> Evidence:
    return Evidence(
        tx,
        "SENT",
        {"step": step, "timestamp": when, "amount_paid": 10.5, "payment_currency": "Euro",
         "payment_format": "Wire", "from_account": f"1:A{step}", "to_account": f"1:A{step + 1}"},
    )  # fmt: skip


def test_path_sentences_claim_a_flow_only_in_time_order() -> None:
    ends = ("cust-A", "cust-B")
    ordered = qa.path_sentences([_hop(2, "t2", "2022-09-02"), _hop(1, "t1", "2022-09-01")], ends, 4)
    assert "in time order" in ordered[0].text and "not in time order" not in ordered[0].text
    assert ordered[0].citations == ["t1", "t2"] and ordered[1].citations == ["t1"]
    unordered = qa.path_sentences(
        [_hop(1, "t1", "2022-09-05"), _hop(2, "t2", "2022-09-01")], ends, 4
    )
    assert "not a traced flow" in unordered[0].text
    none = qa.path_sentences([], ends, 4)
    assert none[0].citations == ["cust-A", "cust-B"] and "No chain of up to 4" in none[0].text


def test_notices_are_fixed_text_for_synthetic_and_icij_evidence() -> None:
    assert qa.notices_for([Evidence("a", "SENT", {})]) == []
    assert qa.notices_for([Evidence("c", "Customer", {})]) == [qa.SYNTHETIC_NOTICE]
    assert qa.notices_for([Evidence("e", "SourceEntity", {"source": "icij"})]) == [qa.ICIJ_NOTICE]


EVIDENCE = [Evidence("10:A", "AccountActivity", {"currency": "Euro", "total": 29335.53})]


def test_draft_is_regenerated_once_with_the_problems_listed() -> None:
    bad = '{"sentences": [{"text": "It sent 99,999 Euro.", "citations": ["10:A"]}]}'
    good = '{"sentences": [{"text": "It sent 29,335.53 Euro.", "citations": ["[10:A]"]}]}'
    provider = ScriptedProvider([bad, good])
    trace: dict[str, Any] = {}
    sentences = qa.draft_answer("How much did 10:A send?", EVIDENCE, _ctx(provider), trace)
    assert sentences and sentences[0].citations == ["10:A"]  # brackets cleaned
    assert "99999 is not in its cited evidence" in provider.prompts[1]
    assert len(trace["drafts"]) == 2


def test_draft_gives_up_after_a_second_failure() -> None:
    bad = '{"sentences": [{"text": "It sent money.", "citations": []}]}'
    provider = ScriptedProvider([bad, bad])
    assert qa.draft_answer("q", EVIDENCE, _ctx(provider), {}) is None


def test_ambiguous_mention_returns_a_question_without_drafting() -> None:
    plan = router.Route(
        intent="customer_profile", mentions=["Morteza Razavi"], start_date=None, end_date=None
    )
    link = Link(
        "Morteza Razavi",
        [Candidate("cust-1", "Customer", "MORTEZA RAZAVI", ("vector",)),
         Candidate("opensanctions:x", "SourceEntity", "Morteza Razavi", ("vector",))],
    )  # fmt: skip
    provider = ScriptedProvider([])  # any LLM call would fail
    with (
        patch.object(qa, "route", return_value=type("R", (), {"value": plan})()),
        patch.object(qa, "retrieve", return_value=([], [link])),
    ):
        response = qa.answer_question("Who is Morteza Razavi?", _ctx(provider))
    assert response.status == "ambiguous"
    assert "cust-1" in response.answer and "opensanctions:x" in response.answer
    assert provider.prompts == [] and response.evidence == []


def test_router_dates_are_parsed() -> None:
    reply = (
        '{"intent": "transactions", "mentions": ["10:A"], "start_date": "2022-09-01",'
        ' "end_date": "2022-09-05"}'
    )
    plan = router.route(
        "What did 10:A send from 1 to 5 September?", ScriptedProvider([reply]), CONFIG
    )
    assert plan.value.start_date == date(2022, 9, 1) and plan.value.end_date == date(2022, 9, 5)


def test_no_path_answer_passes_its_own_grounding_check() -> None:
    plan = router.Route(
        intent="money_path", mentions=["cust-A", "cust-B"], start_date=None, end_date=None
    )
    links = [
        Link("cust-A", [a := Candidate("cust-A", "Customer", "A", ("id",))], a),
        Link("cust-B", [b := Candidate("cust-B", "Customer", "B", ("id",))], b),
    ]
    with (
        patch.object(qa, "route", return_value=type("R", (), {"value": plan})()),
        patch.object(qa, "retrieve", return_value=([], links)),
    ):
        response = qa.answer_question("Is cust-A connected to cust-B?", _ctx(ScriptedProvider([])))
    assert response.status == "answered"
    assert "No chain of up to 4 payments" in response.answer
    assert {e.id for e in response.evidence} == {"cust-A", "cust-B"}
