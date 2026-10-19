"""`make er`: build ER records, embed, block, and score with both model variants (ADR 007).

Outputs go to `data/processed/er/`:
- `predictions_<variant>.parquet`: scored candidate pairs for each variant
  (`splink_no_embeddings` and `splink_full`)
- `candidates.parquet`: every blocked pair, with the keys that found it
- `model_<variant>.json` and `run_manifest.json`

Nothing is written to the graph here. `make er-write` writes edges after thresholds are tuned on
dev, and ground truth is never read.
"""

import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

import polars as pl
from qdrant_client import QdrantClient

from tracegraph.config import Settings, settings
from tracegraph.er.blocking import build_records, candidate_pairs
from tracegraph.er.embeddings import (
    Embedder,
    SentenceTransformerEmbedder,
    index_sources,
    nearest_sources,
)
from tracegraph.er.splink_model import train_and_predict

VARIANTS = {"splink_no_embeddings": False, "splink_full": True}


def run(config: Settings, embedder: Embedder, client: QdrantClient) -> dict[str, Any]:
    started = time.monotonic()
    timings: dict[str, float] = {}
    customers, sources = build_records(config.staging_dir, config.er_block_max_token_frequency)
    timings["records"] = time.monotonic() - started

    source_vectors = index_sources(client, config.qdrant_collection, sources, embedder)
    ann_ids, customer_vectors = nearest_sources(
        client, config.qdrant_collection, customers, embedder, config.er_ann_top_k
    )
    timings["embeddings"] = time.monotonic() - started - sum(timings.values())
    customers = customers.with_columns(
        pl.Series("ann_ids", ann_ids, dtype=pl.List(pl.Utf8)),
        pl.Series("embedding", customer_vectors.tolist(), dtype=pl.List(pl.Float32)),
    )
    sources = sources.with_columns(
        pl.concat_list("unique_id").alias("ann_ids"),
        pl.Series("embedding", source_vectors.tolist(), dtype=pl.List(pl.Float32)),
    )

    config.er_dir.mkdir(parents=True, exist_ok=True)
    candidates = candidate_pairs(customers, sources)
    candidates.write_parquet(config.er_dir / "candidates.parquet")
    counts: dict[str, Any] = {
        "customers": customers.height,
        "sources": sources.height,
        "candidate_pairs": candidates.height,
        "candidate_pairs_by_key": {
            key: int(candidates.filter(pl.col("found_by").list.contains(key)).height)
            for key in ("rare_tokens", "phon_keys", "ann_ids")
        },
    }
    for variant, with_embeddings in VARIANTS.items():
        step = time.monotonic()
        scored, model = train_and_predict(
            customers, sources, config, with_embeddings=with_embeddings
        )
        scored.write_parquet(config.er_dir / f"predictions_{variant}.parquet")
        (config.er_dir / f"model_{variant}.json").write_text(json.dumps(model, indent=2) + "\n")
        counts[f"scored_pairs_{variant}"] = scored.height
        timings[variant] = time.monotonic() - step

    manifest = {
        "run_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "embedding_model": config.embedding_model,
        "counts": counts,
        "timings_seconds": {k: round(v, 1) for k, v in timings.items()},
        "config": {k: v for k, v in config.model_dump(mode="json").items() if k.startswith("er_")},
    }
    (config.er_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    client = QdrantClient(url=settings.qdrant_url)
    manifest = run(settings, SentenceTransformerEmbedder(settings), client)
    print(json.dumps({k: v for k, v in manifest.items() if k != "config"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
