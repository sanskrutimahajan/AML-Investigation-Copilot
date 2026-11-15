"""Grounding check: citations must exist and numbers must come from the cited evidence."""

from dataclasses import dataclass, field

from tracegraph.generation.grounding_check import grounding_problems, numbers
from tracegraph.retrieval.graph_retriever import Evidence


@dataclass
class S:
    text: str
    citations: list[str] = field(default_factory=list)


ACTIVITY = Evidence(
    "10:8000588D0",
    "AccountActivity",
    {"direction": "sent", "currency": "Euro", "total": 29335.53, "transactions": 6,
     "first": "2022-09-01T08:00"},
)  # fmt: skip
USD = Evidence("1:80005E740", "AccountActivity", {"currency": "US Dollar", "total": 5978.69})
EVIDENCE = {e.id: [e] for e in (ACTIVITY, USD)}


def test_numbers_reads_thousands_separators_and_dates() -> None:
    assert numbers("sent 29,335.53 EUR in 6 payments on 2022-09-01") == [
        29335.53, 6, 2022, 9, 1,
    ]  # fmt: skip


def test_grounded_answer_passes_with_rounding() -> None:
    draft = [
        S("Account 10:8000588D0 sent 29,335.53 Euro in 6 transactions.", ["10:8000588D0"]),
        S("That is about 29,336 Euro, starting on 1 September 2022.", ["10:8000588D0"]),
    ]
    assert grounding_problems(draft, EVIDENCE) == []


def test_uncited_unknown_and_invented_numbers_fail() -> None:
    draft = [
        S("The customer is active.", []),
        S("It is linked to cust-999.", ["cust-999"]),
        S("It sent 30,000 Euro.", ["10:8000588D0"]),
    ]
    problems = grounding_problems(draft, EVIDENCE)
    assert any("sentence 1 cites no evidence" in p for p in problems)
    assert any("cust-999" in p for p in problems)
    assert any("30000" in p for p in problems)


def test_a_sum_across_currencies_is_not_grounded() -> None:
    draft = [S("In total it sent 35,314.22.", ["10:8000588D0", "1:80005E740"])]
    assert grounding_problems(draft, EVIDENCE)  # 29,335.53 + 5,978.69 is not in the evidence


def test_a_number_must_come_from_the_evidence_the_sentence_cites() -> None:
    draft = [S("Account 1:80005E740 sent 29,335.53.", ["1:80005E740"])]
    assert grounding_problems(draft, EVIDENCE)


def test_empty_draft_fails() -> None:
    assert grounding_problems([], EVIDENCE) == ["the answer has no sentences"]
