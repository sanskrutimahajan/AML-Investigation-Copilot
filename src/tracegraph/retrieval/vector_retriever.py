"""Name search over Qdrant for entity linking (Phase 3; ADR 009).

The data has no free text (descriptions, notes) to retrieve, so vector search serves one purpose:
finding the customers, source entities and banks whose names resemble a name in the question,
through typos, transliteration and word order. `make qa-index` builds the collection from staging
(normalized names, plus aliases for source entities) with the ER embedding cache.
"""

import sys
from typing import Any

import polars as pl
from qdrant_client import QdrantClient, models

from tracegraph.config import Settings, settings
from tracegraph.er.blocking import build_records
from tracegraph.er.embeddings import (
    CachedEmbedder,
    Embedder,
    SentenceTransformerEmbedder,
    rebuild_collection,
)
from tracegraph.er.normalize import normalize_name


def name_records(config: Settings) -> pl.DataFrame:
    """One row per (node, normalized name variant): `node_id`, `label`, `name`."""
    customers, sources = build_records(config.staging_dir, config.er_block_max_token_frequency)
    banks = pl.read_parquet(config.staging_dir / "ibm_banks.parquet")
    frames = [
        customers.select(pl.col("unique_id").alias("node_id"), pl.lit("Customer").alias("label"),
                         pl.col("names").alias("name")),
        sources.select(pl.col("unique_id").alias("node_id"), pl.lit("SourceEntity").alias("label"),
                       pl.col("names").alias("name")),
    ]  # fmt: skip
    records = pl.concat(frames).explode("name").drop_nulls("name")
    bank_names = banks.select(
        pl.col("bank_id").alias("node_id"),
        pl.lit("Bank").alias("label"),
        pl.col("bank_name").map_elements(normalize_name, return_dtype=pl.Utf8).alias("name"),
    ).filter(pl.col("name") != "")
    return pl.concat([records, bank_names]).unique(maintain_order=True)


def index_names(
    client: QdrantClient, collection: str, records: pl.DataFrame, embedder: Embedder
) -> int:
    vectors = embedder.encode(records["name"].to_list())
    rebuild_collection(client, collection, vectors, records.to_dicts(), ("label",))
    return records.height


def search_names(
    client: QdrantClient,
    collection: str,
    text: str,
    embedder: Embedder,
    limit: int,
    labels: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Distinct nodes whose names are nearest to `text`, best first (`node_id`, `label`, ...)."""
    name = normalize_name(text)
    if not name:
        return []
    query_filter = (
        models.Filter(
            must=[models.FieldCondition(key="label", match=models.MatchAny(any=sorted(labels)))]
        )
        if labels
        else None
    )
    response = client.query_points(
        collection,
        query=embedder.encode([name])[0].tolist(),
        query_filter=query_filter,
        limit=limit * 3,  # several name variants of one node can rank together
        with_payload=True,
    )
    seen: dict[str, dict[str, Any]] = {}
    for point in response.points:
        payload = point.payload or {}
        node = str(payload["node_id"])
        if node not in seen:
            seen[node] = {**payload, "score": float(point.score)}
    return list(seen.values())[:limit]


def main(config: Settings = settings) -> int:
    embedder = CachedEmbedder(
        SentenceTransformerEmbedder(config),
        config.er_dir / "embedding_cache.parquet",
        config.embedding_model,
    )
    records = name_records(config)
    count = index_names(
        QdrantClient(url=config.qdrant_url), config.qdrant_names_collection, records, embedder
    )
    print(f"indexed {count} names in {config.qdrant_names_collection}")
    print(f"embedding cache: {embedder.hits} hits, {embedder.misses} newly encoded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
