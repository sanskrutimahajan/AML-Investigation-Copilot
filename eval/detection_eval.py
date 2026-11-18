"""Typology detection evaluation (ADR 005, ADR 010): `make detect-tune` (dev), `make detect-test`.

Population: seed accounts in one split (`make detect-split`); core seeds are positives, benign seeds
negatives. Non-seed accounts are excluded (ADR 005). Per detector:
- **Pattern recall:** the share of that typology's ground-truth patterns in the split with at least
  one role account flagged. Roles: the hub for fan-in (the receiver with most distinct senders) and
  fan-out (the sender with most distinct receivers); any member for cycles; the source or sink
  (accounts that only send, or only receive, within the pattern) for scatter-gather.
- **Precision:** the share of flagged seed accounts that are core seeds, next to the split's base
  rate. The sample enriches laundering about 19x, so this is not real-world precision.
- **Degree baseline at equal volume:** flag the same number of seed accounts by raw degree
  (in-degree for fan-in, out-degree for fan-out, total otherwise). The paired bootstrap over groups
  gives 95% intervals for detector minus baseline, in precision and in recall.

Dev picks each detector's thresholds by F1 of pattern recall and precision over the config grid.
Test takes them from the latest dev report, requires the config to match, writes
`eval/results/detection_test_final.json` once, and refuses to run again.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from neo4j import GraphDatabase

from tracegraph.config import Settings, settings
from tracegraph.detection import patterns

RESULTS_DIR = Path(__file__).parent / "results"
TEST_RESULT = RESULTS_DIR / "detection_test_final.json"
FULL_DATA = {"laundering_share_of_transactions": 0.001019, "accounts_touching_laundering": 0.0123}
DEGREE = {"FAN-IN": "in", "FAN-OUT": "out", "CYCLE": "total", "SCATTER-GATHER": "total"}
CONFIG_KEYS = {
    "FAN-IN": ("detect_fan_in_min_senders", "detect_fan_in_window_hours"),
    "FAN-OUT": ("detect_fan_out_min_receivers", "detect_fan_out_window_hours"),
    "CYCLE": ("detect_cycle_max_hops", "detect_cycle_window_hours"),
    "SCATTER-GATHER": ("detect_sg_min_intermediates", "detect_sg_window_hours"),
}


def run_detector(edges: pl.DataFrame, typology: str, size: int, window: float) -> pl.DataFrame:
    if typology == "FAN-IN":
        return patterns.fan(edges, "in", size, window)
    if typology == "FAN-OUT":
        return patterns.fan(edges, "out", size, window)
    if typology == "CYCLE":
        return patterns.cycles(edges, size, window)
    return patterns.scatter_gather(edges, size, window)


def grid(typology: str, config: Settings) -> list[tuple[int, float]]:
    sizes = {
        "FAN-IN": config.detect_grid_fan_k,
        "FAN-OUT": config.detect_grid_fan_k,
        "CYCLE": config.detect_grid_cycle_hops,
        "SCATTER-GATHER": config.detect_grid_sg_k,
    }[typology]
    return list(product(sizes, config.detect_grid_window_hours))


def pattern_roles(config: Settings) -> dict[str, list[set[str]]]:
    """Per typology, the role accounts of each ground-truth pattern (see module docstring)."""
    truth = pl.read_parquet(config.ibm_pattern_ground_truth_path).filter(
        pl.col("typology").is_in(list(DEGREE))
    )
    tx = pl.read_parquet(config.staging_dir / "ibm_transactions.parquet")
    rows = truth.join(tx, on="transaction_id")
    roles: dict[str, list[set[str]]] = {t: [] for t in DEGREE}
    for (_, typology), group in rows.group_by("pattern_id", "typology", maintain_order=True):
        senders, receivers = set(group["from_account_id"]), set(group["to_account_id"])
        if typology == "FAN-IN":
            hub = group.group_by("to_account_id").agg(pl.col("from_account_id").n_unique())
            role = {hub.sort("from_account_id", "to_account_id", descending=[True, False])[0, 0]}
        elif typology == "FAN-OUT":
            hub = group.group_by("from_account_id").agg(pl.col("to_account_id").n_unique())
            role = {hub.sort("to_account_id", "from_account_id", descending=[True, False])[0, 0]}
        elif typology == "CYCLE":
            role = senders | receivers
        else:
            role = (senders - receivers) | (receivers - senders)
        roles[str(typology)].append(role)
    return roles


def degrees(edges: pl.DataFrame) -> dict[str, dict[str, int]]:
    ins = dict(edges.group_by("target").len().iter_rows())
    outs = dict(edges.group_by("source").len().iter_rows())
    total = {a: ins.get(a, 0) + outs.get(a, 0) for a in set(ins) | set(outs)}
    return {"in": ins, "out": outs, "total": total}


def baseline(population: pl.DataFrame, degree: dict[str, int], n: int) -> set[str]:
    """The `n` accounts of the population with the highest degree (ties: account id)."""
    ranked = population.select("account_id").with_columns(
        pl.col("account_id").replace_strict(degree, default=0, return_dtype=pl.Int64).alias("d")
    )
    return set(ranked.sort(["d", "account_id"], descending=[True, False]).head(n)["account_id"])


def score(flagged: set[str], population: pl.DataFrame, roles: list[set[str]]) -> dict[str, Any]:
    in_split = set(population["account_id"])
    core = set(population.filter(pl.col("seed_type") == "core")["account_id"])
    hits = flagged & in_split
    patterns_here = [r for r in roles if r & in_split]
    caught = sum(bool(r & hits) for r in patterns_here)
    precision = len(hits & core) / len(hits) if hits else 0.0
    recall = caught / len(patterns_here) if patterns_here else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "flagged_seed_accounts": len(hits),
        "precision": round(precision, 4),
        "patterns": len(patterns_here),
        "patterns_caught": caught,
        "pattern_recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def paired_bootstrap(
    flagged: set[str],
    base: set[str],
    population: pl.DataFrame,
    roles: list[set[str]],
    config: Settings,
) -> dict[str, list[float]]:
    """95% CIs for detector minus baseline (precision, recall), resampling account groups."""
    groups = population["group"].unique().sort().to_list()
    index = {g: i for i, g in enumerate(groups)}
    group_of = dict(population.select("account_id", "group").iter_rows())
    core = set(population.filter(pl.col("seed_type") == "core")["account_id"])
    # Per group: [det flagged, det flagged core, base flagged, base flagged core,
    #             patterns, det caught, base caught]
    table = np.zeros((len(groups), 7))
    for account, group in group_of.items():
        row = index[group]
        table[row] += [account in flagged, account in flagged and account in core,
                       account in base, account in base and account in core, 0, 0, 0]  # fmt: skip
    for role in roles:
        members = [a for a in role if a in group_of]
        if members:
            row = index[group_of[members[0]]]
            table[row, 4:] += [1, bool(role & flagged), bool(role & base)]
    rng = np.random.default_rng(config.random_seed)
    diffs = []
    for _ in range(config.detect_bootstrap_samples):
        s = table[rng.integers(0, len(groups), len(groups))].sum(axis=0)
        p_det, p_base = s[1] / max(s[0], 1), s[3] / max(s[2], 1)
        r_det, r_base = s[5] / max(s[4], 1), s[6] / max(s[4], 1)
        diffs.append((p_det - p_base, r_det - r_base))
    arr = np.array(diffs)
    return {
        "precision_diff_ci95": [round(float(np.percentile(arr[:, 0], q)), 4) for q in (2.5, 97.5)],
        "recall_diff_ci95": [round(float(np.percentile(arr[:, 1], q)), 4) for q in (2.5, 97.5)],
    }


def evaluate(
    config: Settings, split_name: str, chosen: dict[str, list[float]] | None
) -> dict[str, Any]:
    split = pl.read_parquet(config.detection_split_path)
    population = split.filter(pl.col("split") == split_name)
    driver = GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)
    )
    with driver:
        edges = patterns.load_edges(driver, config.neo4j_database)
    roles, degree = pattern_roles(config), degrees(edges)
    base_rate = population.filter(pl.col("seed_type") == "core").height / population.height
    report: dict[str, Any] = {
        "split": split_name,
        "population": {
            "seed_accounts": population.height,
            "core": int((population["seed_type"] == "core").sum()),
            "base_rate_core": round(base_rate, 4),
            "full_data": FULL_DATA,
            "note": "The sample enriches laundering about 19x (ADR 005); sample precision is not "
            "real-world precision.",
        },
        "detectors": {},
    }
    tuning = chosen is None
    chosen = chosen or {}
    for typology in DEGREE:
        entry: dict[str, Any] = {}
        if tuning:
            results = []
            for size, window in grid(typology, config):
                alerts = run_detector(edges, typology, size, window)
                flagged = set(alerts["accounts"].explode().drop_nulls())
                results.append({"size": size, "window_hours": window,
                                **score(flagged, population, roles[typology])})  # fmt: skip
            best = max(results, key=lambda r: (r["f1"], -r["flagged_seed_accounts"]))
            chosen[typology] = [best["size"], best["window_hours"]]
            entry["grid"] = results
        size, window = int(chosen[typology][0]), float(chosen[typology][1])
        alerts = run_detector(edges, typology, size, window)
        flagged = set(alerts["accounts"].explode().drop_nulls())
        result = score(flagged, population, roles[typology])
        base = baseline(population, degree[DEGREE[typology]], result["flagged_seed_accounts"])
        entry.update(
            {
                "size": size,
                "window_hours": window,
                "alerts_all_accounts": alerts.height,
                "flagged_accounts_all": len(flagged),
                "detector": result,
                "degree_baseline": score(base, population, roles[typology]),
                "detector_minus_baseline": paired_bootstrap(
                    flagged, base, population, roles[typology], config
                ),
            }
        )
        report["detectors"][typology] = entry
    report["thresholds"] = chosen
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    args = parser.parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if args.split == "test":
        if TEST_RESULT.exists():
            print(f"{TEST_RESULT} exists: the test split has already been scored. Refusing.")
            return 2
        dev_report = max(RESULTS_DIR.glob("detection_dev_*.json"))
        chosen = json.loads(dev_report.read_text())["thresholds"]
        mismatch = {
            t: (v, [getattr(settings, k) for k in CONFIG_KEYS[t]])
            for t, v in chosen.items()
            if [float(x) for x in v] != [float(getattr(settings, k)) for k in CONFIG_KEYS[t]]
        }
        if mismatch:
            print(f"Config thresholds differ from {dev_report.name}: {mismatch}. Set them first.")
            return 2
        report = evaluate(settings, "test", chosen)
        report["thresholds_from"] = dev_report.name
        path = TEST_RESULT
    else:
        report = evaluate(settings, "dev", None)
        path = RESULTS_DIR / f"detection_dev_{stamp}.json"
    report["run_utc"] = stamp
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    summary = {t: {k: e[k] for k in ("size", "window_hours", "detector", "degree_baseline",
                                       "detector_minus_baseline")}  # fmt: skip
               for t, e in report["detectors"].items()}  # fmt: skip
    print(json.dumps({"population": report["population"], "detectors": summary}, indent=2))
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
