"""End-to-end ER scoring on fixture staging (fake embedder, in-memory Qdrant)."""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from qdrant_client import QdrantClient

import tracegraph.er
from tracegraph.config import Settings
from tracegraph.er import run as er_run


class HashEmbedder:
    """Deterministic character-trigram hashing embedder (no model download)."""

    dimension = 64

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            padded = f"  {text}  "
            for i in range(len(padded) - 2):
                out[row, hash(padded[i : i + 3]) % self.dimension] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.maximum(norms, 1e-9)


@pytest.fixture(scope="module")
def er_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Staging built from the 600-customer seeding fixture (EM needs more than a handful)."""
    import test_seed_links

    data_dir = tmp_path_factory.mktemp("er_data")
    config = Settings(
        _env_file=None,
        data_dir=data_dir,
        seeded_link_rate=0.3,
        er_u_max_pairs=20_000,
        er_prediction_floor=0.0,
        er_block_max_token_frequency=60,
    )
    inputs = test_seed_links.seeding_inputs.__wrapped__()
    build = test_seed_links._build(inputs, config)
    config.staging_dir.mkdir(parents=True)
    build.customers.write_parquet(config.staging_dir / "customers.parquet")
    inputs["opensanctions"].write_parquet(config.staging_dir / "opensanctions_entities.parquet")
    inputs["icij"].with_columns(pl.lit("Panama Papers").alias("source_leak")).write_parquet(
        config.staging_dir / "icij_nodes.parquet"
    )
    er_run.run(config, HashEmbedder(), QdrantClient(":memory:"))
    return config.er_dir


def test_er_run_writes_scored_pairs_for_both_variants(er_output: Path) -> None:
    for variant in er_run.VARIANTS:
        scored = pl.read_parquet(er_output / f"predictions_{variant}.parquet")
        assert {"customer_id", "entity_id", "probability", "match_weight"} <= set(scored.columns)
        assert scored["probability"].is_between(0, 1).all()
        assert scored["customer_id"].str.starts_with("cust-").all()
        assert not scored["entity_id"].str.starts_with("cust-").any()
    assert (er_output / "run_manifest.json").exists()
    candidates = pl.read_parquet(er_output / "candidates.parquet")
    assert candidates.filter(pl.col("found_by").list.contains("ann_ids")).height > 0


def test_er_modules_never_read_ground_truth() -> None:
    er_dir = Path(tracegraph.er.__file__).parent
    for module in er_dir.glob("*.py"):
        source = module.read_text()
        for forbidden in ("ground_truth", "is_laundering", "is_match", "er_split", "seed_reason"):
            assert forbidden not in source, (module.name, forbidden)
