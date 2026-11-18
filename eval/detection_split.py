"""Dev/test split of seed accounts for typology detection (`make detect-split`, ADR 010).

The population is seed accounts only (ADR 005): core seeds (touching a laundering or pattern
transaction) and benign seeds. The split is 50/50, stratified by seed type and grouped by
connected component of seeds that share a laundering transaction, so no pattern has accounts in
both halves. Thresholds are tuned on dev; test is scored once. The split is written once and never
regenerated silently.
"""

import sys

import numpy as np
import polars as pl
from sklearn.model_selection import StratifiedGroupKFold

from tracegraph.config import Settings, settings


def _components(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Union-find: account -> representative of its connected component."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[find(a)] = find(b)
    return {x: find(x) for x in parent}


def make_split(config: Settings) -> pl.DataFrame:
    seeds = pl.read_parquet(config.processed_dir / "ibm_sample_seeds.parquet")
    truth = pl.read_parquet(config.ibm_pattern_ground_truth_path).select("transaction_id")
    tx = pl.read_parquet(config.staging_dir / "ibm_transactions.parquet")
    seed_ids = set(seeds["account_id"])
    pairs = [
        (a, b)
        for a, b in truth.join(tx, on="transaction_id")
        .select("from_account_id", "to_account_id")
        .iter_rows()
        if a in seed_ids and b in seed_ids
    ]
    component = _components(pairs)
    frame = seeds.with_columns(
        pl.col("account_id").replace_strict(component, default=pl.col("account_id")).alias("group")
    ).sort("account_id")
    folds = StratifiedGroupKFold(n_splits=2, shuffle=True, random_state=config.random_seed)
    _, test_index = next(folds.split(np.zeros(frame.height), frame["seed_type"], frame["group"]))
    split = np.full(frame.height, "dev", dtype=object)
    split[test_index] = "test"
    return frame.with_columns(pl.Series("split", split.tolist())).select(
        "account_id", "split", "seed_type", "group"
    )


def main() -> int:
    path = settings.detection_split_path
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
    print(split.group_by("split", "seed_type").len().sort("seed_type", "split"))
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
