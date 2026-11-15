"""Entity linking: IDs and names in a question -> graph nodes (Phase 3; ADR 009).

- An ID (`cust-...` customer, `<bank>:<hex>` account, `opensanctions:` / `icij:` entity) is looked
  up directly, also inside a longer mention ("account 10:8000588D0"). A mention that is exactly an
  unknown ID is not found; an unknown ID inside a longer mention falls back to name search.
- A name is searched two ways, merged by reciprocal rank fusion: the Neo4j full-text index
  `party_names` (fuzzy terms; customers and source entities) and Qdrant name embeddings
  (customers, source entities and banks).
- A node found only by vector search is kept only at `qa_link_min_vector_score` or above:
  nearest neighbours always exist, even for nonsense.
- A node is chosen only when that is unambiguous: exactly one candidate whose name or alias equals
  the mention (same words, any order), or, failing that, the same node ranked first by both
  searches. Otherwise the caller asks which one was meant: homonyms exist by design.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from neo4j import Driver
from qdrant_client import QdrantClient

from tracegraph.config import Settings
from tracegraph.er.embeddings import Embedder
from tracegraph.er.normalize import normalize_name
from tracegraph.retrieval.fusion import rrf
from tracegraph.retrieval.graph_retriever import run_read
from tracegraph.retrieval.vector_retriever import search_names

ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "Customer": re.compile(r"cust-[0-9A-F]+"),
    "Account": re.compile(r"\d+:[0-9A-F]+"),
    "SourceEntity": re.compile(r"(?:opensanctions|icij):[\w.-]+"),
}
_LOOKUP = {
    "Customer": "MATCH (n:Customer {customer_id: $id}) RETURN n.name AS name LIMIT 1",
    "Account": "MATCH (n:Account {account_id: $id}) RETURN n.account_id AS name LIMIT 1",
    "SourceEntity": "MATCH (n:SourceEntity {entity_id: $id}) RETURN n.name AS name LIMIT 1",
}
_FULLTEXT = """
CALL db.index.fulltext.queryNodes('party_names', $query, {limit: $limit}) YIELD node
RETURN coalesce(node.customer_id, node.entity_id) AS id LIMIT $limit
"""
_NAMES = """
UNWIND $ids AS id
OPTIONAL MATCH (c:Customer {customer_id: id})
OPTIONAL MATCH (e:SourceEntity {entity_id: id})
OPTIONAL MATCH (b:Bank {bank_id: id})
WITH id, c, e, b WHERE c IS NOT NULL OR e IS NOT NULL OR b IS NOT NULL
RETURN id,
       CASE WHEN c IS NOT NULL THEN 'Customer' WHEN e IS NOT NULL THEN 'SourceEntity'
            ELSE 'Bank' END AS label,
       coalesce(c.name, e.name, b.name) AS name, coalesce(e.aliases, []) AS aliases
LIMIT $limit
"""


@dataclass(frozen=True)
class Candidate:
    node_id: str
    label: str
    name: str
    via: tuple[str, ...]


@dataclass
class Link:
    mention: str
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Candidate | None = None

    @property
    def ambiguous(self) -> bool:
        return self.chosen is None and bool(self.candidates)


def _words(name: str) -> str:
    return " ".join(sorted(normalize_name(name).split()))


def lucene_query(mention: str) -> str:
    """Fuzzy OR query from normalized words (only [a-z0-9] reach Lucene, so nothing to escape)."""
    words = re.findall(r"[a-z0-9]+", normalize_name(mention))
    return " ".join(f"{w}~1" if len(w) >= 4 else w for w in words)


def link_mention(
    mention: str,
    driver: Driver,
    database: str,
    client: QdrantClient,
    embedder: Embedder,
    config: Settings,
    labels: frozenset[str] | None = None,
) -> Link:
    text = mention.strip()
    for label, pattern in ID_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        whole = match.group(0) == text  # the mention is just the ID
        if labels and label not in labels:
            if whole:
                return Link(mention)
            continue
        hit = run_read(driver, database, _LOOKUP[label], {"id": match.group(0)}, config)
        if hit:
            found = Candidate(match.group(0), label, str(hit[0]["name"]), ("id",))
            return Link(mention, [found], found)
        if whole:
            return Link(mention)

    k = config.qa_link_top_k
    query = lucene_query(text)
    fulltext = (
        [
            r["id"]
            for r in run_read(driver, database, _FULLTEXT, {"query": query, "limit": k * 4}, config)
        ]
        if query
        else []
    )
    hits = search_names(client, config.qdrant_names_collection, text, embedder, k * 2, labels)
    vector = [str(hit["node_id"]) for hit in hits]
    strong = {str(h["node_id"]) for h in hits if h["score"] >= config.qa_link_min_vector_score}
    fused = [
        node
        for node, _ in rrf([fulltext, vector], config.qa_rrf_k)
        if node in fulltext or node in strong
    ]
    rows: list[dict[str, Any]] = run_read(
        driver, database, _NAMES, {"ids": fused, "limit": len(fused) or 1}, config
    )
    info = {row["id"]: row for row in rows if not labels or row["label"] in labels}
    candidates = [
        Candidate(
            node,
            info[node]["label"],
            str(info[node]["name"]),
            tuple(s for s, ids in (("fulltext", fulltext), ("vector", vector)) if node in ids),
        )
        for node in fused
        if node in info
    ][:k]
    target = _words(text)
    exact = [
        c
        for c in candidates
        if target in {_words(n) for n in [c.name, *info[c.node_id]["aliases"]] if n}
    ]
    chosen = None
    if len(exact) == 1:
        chosen = exact[0]
    elif not exact:
        top_fulltext = next((n for n in fulltext if n in info), None)
        top_vector = next((n for n in vector if n in info), None)
        if top_fulltext and top_fulltext == top_vector:
            chosen = next((c for c in candidates if c.node_id == top_fulltext), None)
    return Link(mention, candidates, chosen)
