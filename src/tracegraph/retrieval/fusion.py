"""Fusion: reciprocal rank fusion for linking candidates, and evidence selection (Phase 3)."""

from tracegraph.retrieval.graph_retriever import Evidence


def rrf(rankings: list[list[str]], k: int) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: score(id) = sum over rankings of 1 / (k + rank), best first."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(dict.fromkeys(ranking), start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def select_evidence(items: list[Evidence], max_items: int) -> list[Evidence]:
    """Drop exact duplicates, keep retrieval order, and cap the number of items."""
    seen: set[str] = set()
    kept = []
    for item in items:
        key = item.render()
        if key not in seen:
            seen.add(key)
            kept.append(item)
    return kept[:max_items]
