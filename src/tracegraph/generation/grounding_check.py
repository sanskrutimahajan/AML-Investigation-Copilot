"""Grounding check for drafted answers (Phase 3).

A draft passes only if every sentence:
- cites at least one ID;
- cites only IDs present in the retrieved evidence;
- uses only numbers that appear in the evidence it cites or in the question (rounding allowed:
  within 0.5 or 0.1%). Digits inside IDs (`10:8000588D0`) are not numbers.
The number rule catches invented figures and sums the evidence does not contain, such as a total
across currencies. On failure the answer step regenerates once, then says "insufficient evidence".
"""

import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from tracegraph.retrieval.graph_retriever import Evidence

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ID = re.compile(
    r"cust-[0-9A-F]+|\b\d+:[0-9A-F]+\b|ibm-\d+|icij-rel-\d+|(?:opensanctions|icij):[\w.-]+"
)


class SentenceLike(Protocol):
    text: str
    citations: list[str]


def numbers(text: str) -> list[float]:
    """Every number in the text; commas are read as thousands separators."""
    bare = _ID.sub(" ", text)
    return [float(match.replace(",", "")) for match in _NUMBER.findall(bare) if match.strip(",")]


def clean_citation(citation: str) -> str:
    """Models often wrap IDs in brackets or quotes, as the evidence lines show them."""
    return citation.strip().strip("[]\"' ")


def _supported(value: float, allowed: list[float]) -> bool:
    return any(abs(value - a) <= max(0.5, 0.001 * abs(a)) for a in allowed)


def grounding_problems(
    sentences: Sequence[SentenceLike],
    evidence: Mapping[str, list[Evidence]],
    question: str = "",
) -> list[str]:
    """Problems with a draft (empty if grounded). `evidence` maps each ID to its items; citations
    must already be cleaned (`clean_citation`)."""
    if not sentences:
        return ["the answer has no sentences"]
    problems = []
    for i, sentence in enumerate(sentences, start=1):
        if not sentence.citations:
            problems.append(f"sentence {i} cites no evidence")
            continue
        unknown = [c for c in sentence.citations if c not in evidence]
        if unknown:
            problems.append(f"sentence {i} cites IDs not in the evidence: {', '.join(unknown)}")
        allowed = numbers(question) + [
            n
            for c in sentence.citations
            for item in evidence.get(c, [])
            for n in numbers(item.render())
        ]
        for value in numbers(sentence.text):
            if not _supported(value, allowed):
                problems.append(f"sentence {i}: the number {value:g} is not in its cited evidence")
    return problems
