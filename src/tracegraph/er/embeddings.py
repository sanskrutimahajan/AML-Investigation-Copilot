"""Name embeddings in Qdrant, used for nearest-neighbour blocking and a cosine comparison (ADR 007).

Every source name variant (primary name and aliases) is indexed with its entity ID and kind.
Customers are searched against variants of their own kind, and each customer's `ann_ids` are its
top-k distinct entities. The collection is derived data and is rebuilt on every ER run.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import polars as pl
from qdrant_client import QdrantClient, models

from tracegraph.config import Settings


class Embedder(Protocol):
    dimension: int

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """L2-normalized float32 vectors, one row per text."""
        ...


class SentenceTransformerEmbedder:
    """Loads `embedding_model` lazily (a one-time download, then cached locally)."""

    def __init__(self, config: Settings) -> None:
        self._model_name = config.embedding_model
        self._batch_size = config.er_embedding_batch_size
        self._model: Any = None
        self.dimension = 0

    def _load(self) -> Any:
        if self._model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._model = SentenceTransformer(self._model_name, device=device)
            self.dimension = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        model = self._load()
        vectors = model.encode(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


class CachedEmbedder:
    """Wraps an embedder with a per-model Parquet cache keyed by text (derived data, local only)."""

    def __init__(self, inner: Embedder, cache_path: Path, model_name: str) -> None:
        self._inner = inner
        self._path = cache_path
        self._model = model_name
        self._cache: dict[str, np.ndarray] = {}
        if cache_path.exists():
            frame = pl.read_parquet(cache_path).filter(pl.col("model") == model_name)
            self._cache = {
                t: np.asarray(v, dtype=np.float32)
                for t, v in frame.select("text", "vector").iter_rows()
            }
        self.dimension = inner.dimension
        self.hits = self.misses = 0

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        missing = list(dict.fromkeys(t for t in texts if t not in self._cache))
        self.misses += len(missing)
        self.hits += len(texts) - len(missing)
        if missing:
            for text, vector in zip(missing, self._inner.encode(missing), strict=True):
                self._cache[text] = vector
            self._save()
        vectors = np.stack([self._cache[t] for t in texts]) if texts else np.zeros((0, 0))
        self.dimension = int(vectors.shape[1]) if len(texts) else self.dimension
        return vectors.astype(np.float32)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "model": [self._model] * len(self._cache),
                "text": list(self._cache),
                "vector": [v.tolist() for v in self._cache.values()],
            },
            schema={"model": pl.Utf8, "text": pl.Utf8, "vector": pl.List(pl.Float32)},
        ).write_parquet(self._path)


def rebuild_collection(
    client: QdrantClient,
    collection: str,
    vectors: np.ndarray,
    payloads: list[dict[str, Any]],
    keyword_fields: tuple[str, ...],
) -> None:
    """Drop and recreate a cosine collection holding one point per (vector, payload)."""
    if client.collection_exists(collection):
        client.delete_collection(collection)
    client.create_collection(
        collection,
        vectors_config=models.VectorParams(size=vectors.shape[1], distance=models.Distance.COSINE),
    )
    for field in keyword_fields:
        client.create_payload_index(collection, field, models.PayloadSchemaType.KEYWORD)
    batch = 1024
    for start in range(0, len(payloads), batch):
        client.upsert(
            collection,
            points=[
                models.PointStruct(id=i, vector=vectors[i].tolist(), payload=payloads[i])
                for i in range(start, min(start + batch, len(payloads)))
            ],
            wait=True,
        )


def index_sources(
    client: QdrantClient, collection: str, sources: pl.DataFrame, embedder: Embedder
) -> np.ndarray:
    """(Re)build the collection over every source name variant; returns primary-name vectors."""
    variants = sources.select("unique_id", "kind", "names").explode("names").drop_nulls("names")
    vectors = embedder.encode(variants["names"].to_list())
    payloads = [
        {"entity_id": row["unique_id"], "kind": row["kind"], "name": row["names"]}
        for row in variants.iter_rows(named=True)
    ]
    rebuild_collection(client, collection, vectors, payloads, ("kind",))
    # The first variant of each entity is its primary name.
    first = variants.with_row_index("row").group_by("unique_id", maintain_order=True).first()
    order = sources.select("unique_id").join(first, on="unique_id", how="left")["row"].to_numpy()
    return vectors[order]


def nearest_sources(
    client: QdrantClient,
    collection: str,
    customers: pl.DataFrame,
    embedder: Embedder,
    top_k: int,
) -> tuple[list[list[str]], np.ndarray]:
    """(top-k distinct entity IDs per customer, customer name vectors)."""
    vectors = embedder.encode(customers["name_norm"].to_list())
    kinds = customers["kind"].to_list()
    results: list[list[str]] = []
    batch = 256
    for start in range(0, len(kinds), batch):
        requests = [
            models.QueryRequest(
                query=vectors[i].tolist(),
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="kind", match=models.MatchValue(value=kinds[i]))
                    ]
                ),
                limit=top_k * 3,  # several variants of one entity can rank together
                with_payload=["entity_id"],
            )
            for i in range(start, min(start + batch, len(kinds)))
        ]
        for response in client.query_batch_points(collection, requests=requests):
            ids = list(
                dict.fromkeys(str(p.payload["entity_id"]) for p in response.points if p.payload)
            )
            results.append(ids[:top_k])
    return results, vectors
