"""Dev/test split of customers for ER evaluation (`make er-split`, ADR 007).

50/50 split, grouped by `source_entity_id`, so no source entity has customers in both halves
(a plain customer is its own group), and stratified by role and `seed_reason`. Every threshold and
setting is tuned on dev; test is scored once, for the final numbers. The split is written once and
never regenerated silently.
"""

import sys

import numpy as np
import polars as pl
from sklearn.model_selection import StratifiedGroupKFold

from tracegraph.config import Settings, settings


def make_split(config: Settings) -> pl.DataFrame:
    customers = pl.read_parquet(config.staging_dir / "customers.parquet").select("customer_id")
    truth = pl.read_parquet(config.seed_ground_truth_path).select(
        "customer_id", "link_type", "seed_reason", "source_entity_id"
    )
    frame = (
        customers.join(truth, on="customer_id", how="left")
        .with_columns(
            pl.col("link_type").fill_null("plain"),
            pl.col("seed_reason").fill_null("none"),
            pl.coalesce("source_entity_id", "customer_id").alias("group"),
        )
        .with_columns(pl.format("{}|{}", "link_type", "seed_reason").alias("stratum"))
        .sort("customer_id")
    )
    folds = StratifiedGroupKFold(n_splits=2, shuffle=True, random_state=config.random_seed)
    _, test_index = next(folds.split(np.zeros(frame.height), frame["stratum"], frame["group"]))
    split = np.full(frame.height, "dev", dtype=object)
    split[test_index] = "test"
    return frame.with_columns(pl.Series("split", split.tolist())).select(
        "customer_id", "split", "link_type", "seed_reason", "group"
    )


def main() -> int:
    path = settings.er_split_path
    if path.exists():
        print(f"{path} already exists; the split is fixed once made. Not regenerating.")
        return 0
    split = make_split(settings)
    overlap = (
        split.group_by("group").agg(pl.col("split").n_unique().alias("n")).filter(pl.col("n") > 1)
    )
    if overlap.height:
        raise RuntimeError(f"{overlap.height} groups appear in both halves")
    split.write_parquet(path)
    print(split.group_by("split", "link_type", "seed_reason").len().sort("link_type", "split"))
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
