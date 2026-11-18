"""Phase 4 detector evaluation (ADR 015): `make detect-tune-ext` (dev), `make detect-test-ext`.

Same population, split, scoring, degree baseline and paired bootstrap as `detection_eval.py`
(ADR 010); the original four detectors and their frozen test result are untouched. Ground truth,
agreed with the user on 2026-11-18:

- **Layering:** IBM STACK patterns. A hit flags an account that both receives and sends within the
  pattern. Stacks with no such account are left out and counted.
- **Mule hub:** IBM GATHER-SCATTER patterns. A hit flags a hub (an account that both receives and
  sends within the pattern).
- **Rapid movement:** no IBM label. Precision only (share of flagged seed accounts that are core
  seeds) against the degree baseline at equal volume; no recall. Dev picks the grid point with the
  largest precision gain among points flagging at least `detect_rapid_min_flagged` seed accounts.
- **Structuring:** no IBM label. Our own generator injects cases into a copy of the real edges:
  sub-threshold cash deposits from distinct senders, plus three kinds of decoy (above threshold,
  spread out in time, not cash). Recall on injected cases and decoy false positives test only the
  detector's logic, not real-world performance. Its thresholds are fixed by config, not tuned.
  Real-data alert volume is reported too.
- IBM BIPARTITE patterns are not used: in this data they are separate one-to-one transfers with no
  account on both sides, not a network a graph pattern can find.

Dev writes `detection_ext_dev_<stamp>.json`. Test takes thresholds from the latest dev report,
requires the config to match, writes `detection_ext_test_final.json` once, and refuses to run again.
"""

import argparse
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path
from typing import Any

import polars as pl
from detection_eval import FULL_DATA, baseline, degrees, paired_bootstrap, score
from neo4j import GraphDatabase

from tracegraph.config import Settings, settings
from tracegraph.detection import patterns

RESULTS_DIR = Path(__file__).parent / "results"
TEST_RESULT = RESULTS_DIR / "detection_ext_test_final.json"
TUNED = ("LAYERING", "MULE-HUB", "RAPID-MOVEMENT")
GROUND_TRUTH = {"LAYERING": "STACK", "MULE-HUB": "GATHER-SCATTER"}
CONFIG_KEYS = {
    "LAYERING": ("detect_layer_min_hops", "detect_layer_window_hours"),
    "MULE-HUB": ("detect_mule_min_senders", "detect_mule_window_hours"),
    "RAPID-MOVEMENT": ("detect_rapid_min_pass_throughs", "detect_rapid_window_hours"),
}


def run_detector(edges: pl.DataFrame, typology: str, size: int, window: float,
                 config: Settings) -> pl.DataFrame:  # fmt: skip
    if typology == "LAYERING":
        return patterns.layering(edges, size, window)
    if typology == "MULE-HUB":
        return patterns.mule_hub(edges, size, window)
    return patterns.rapid_movement(edges, size, window, config.detect_rapid_min_ratio)


def grid(typology: str, config: Settings) -> list[tuple[int, float]]:
    sizes = {"LAYERING": config.detect_grid_layer_hops, "MULE-HUB": config.detect_grid_mule_k,
             "RAPID-MOVEMENT": config.detect_grid_rapid_n}[typology]  # fmt: skip
    return list(product(sizes, config.detect_grid_window_hours))


def pattern_roles(config: Settings) -> tuple[dict[str, list[set[str]]], dict[str, int]]:
    """Role accounts per pattern (accounts that both receive and send), and patterns left out."""
    truth = pl.read_parquet(config.ibm_pattern_ground_truth_path).filter(
        pl.col("typology").is_in(list(GROUND_TRUTH.values()))
    )
    tx = pl.read_parquet(config.staging_dir / "ibm_transactions.parquet")
    rows = truth.join(tx, on="transaction_id")
    roles: dict[str, list[set[str]]] = {t: [] for t in GROUND_TRUTH}
    left_out = dict.fromkeys(GROUND_TRUTH, 0)
    by_ibm = {v: k for k, v in GROUND_TRUTH.items()}
    for (_, ibm_type), group in rows.group_by("pattern_id", "typology", maintain_order=True):
        role = set(group["from_account_id"]) & set(group["to_account_id"])
        if role:
            roles[by_ibm[str(ibm_type)]].append(role)
        else:
            left_out[by_ibm[str(ibm_type)]] += 1
    return roles, left_out


def _pick(typology: str, results: list[dict[str, Any]], config: Settings) -> dict[str, Any]:
    if typology != "RAPID-MOVEMENT":
        return max(results, key=lambda r: (r["f1"], -r["flagged_seed_accounts"]))
    eligible = [r for r in results if r["flagged_seed_accounts"] >= config.detect_rapid_min_flagged]
    pool = eligible or results
    return max(pool, key=lambda r: (r["precision_gain"], -r["flagged_seed_accounts"]))


def inject_structuring(
    edges: pl.DataFrame, config: Settings
) -> tuple[pl.DataFrame, dict[str, set[str]]]:
    """A copy of the edges with injected structuring cases and decoys (fixed seed)."""
    rng = random.Random(config.random_seed)
    t0, threshold, band = (
        datetime(2022, 9, 2),
        config.detect_struct_threshold,
        config.detect_struct_band,
    )
    span = config.detect_struct_window_hours
    rows: list[dict[str, Any]] = []
    groups: dict[str, set[str]] = {"injected": set(), "above_threshold": set(),
                                   "spread_out": set(), "not_cash": set()}  # fmt: skip

    def add(kind: str, i: int, k: int, amount: Any, gap_hours: float, fmt: str) -> None:
        account = f"9{len(groups)}{i:03d}:INJ{kind[:3].upper()}"
        groups[kind].add(account)
        start = t0 + timedelta(hours=rng.uniform(0, 24 * 5))
        for j in range(k):
            paid = round(amount(), 2)
            rows.append({"transaction_id": f"inj-{kind}-{i}-{j}", "source": f"8{i:03d}{j}:SRC",
                         "target": account, "timestamp": start + timedelta(hours=j * gap_hours),
                         "amount_paid": paid, "payment_currency": config.detect_struct_currency,
                         "amount_received": paid,
                         "receiving_currency": config.detect_struct_currency,
                         "payment_format": fmt})  # fmt: skip

    def in_band() -> float:
        return rng.uniform(threshold * (1 - band), threshold - 0.01)

    def above() -> float:
        return rng.uniform(threshold, threshold * 1.4)

    lo = config.detect_struct_min_deposits
    for i in range(config.detect_struct_injected_cases):
        k = rng.randint(lo, lo + 3)
        tight = span * 0.8 / max(k - 1, 1)
        add("injected", i, k, in_band, tight, "Cash")
        add("above_threshold", i, k, above, tight, "Cash")
        add("spread_out", i, k, in_band, span * 1.5, "Cash")
        add("not_cash", i, k, in_band, tight, "Wire")
    injected = pl.DataFrame(rows).cast(edges.schema)  # type: ignore[arg-type]
    return pl.concat([edges, injected]).sort("timestamp", "transaction_id"), groups


def structuring_report(edges: pl.DataFrame, population: pl.DataFrame,
                       config: Settings) -> dict[str, Any]:  # fmt: skip
    params = (config.detect_struct_min_deposits, config.detect_struct_window_hours,
              config.detect_struct_threshold, config.detect_struct_band,
              config.detect_struct_currency)  # fmt: skip
    real = set(patterns.structuring(edges, *params)["accounts"].explode().drop_nulls())
    combined, groups = inject_structuring(edges, config)
    flagged = set(patterns.structuring(combined, *params)["accounts"].explode().drop_nulls())
    core = set(population.filter(pl.col("seed_type") == "core")["account_id"])
    in_split = set(population["account_id"])
    return {
        "thresholds": dict(zip(("min_deposits", "window_hours", "threshold", "band", "currency"),
                               params, strict=True)),
        "note": "Injected by our own generator: tests the detector's logic only.",
        "injected_cases": len(groups["injected"]),
        "injected_caught": len(flagged & groups["injected"]),
        "injected_recall": round(len(flagged & groups["injected"]) / len(groups["injected"]), 4),
        "decoy_false_positives": {kind: f"{len(flagged & accounts)}/{len(accounts)}"
                                  for kind, accounts in groups.items() if kind != "injected"},
        "real_data": {"accounts_flagged": len(real), "seed_accounts_in_split": len(real & in_split),
                      "core_seeds_in_split": len(real & in_split & core)},
    }  # fmt: skip


def evaluate(config: Settings, split_name: str,
             chosen: dict[str, list[float]] | None) -> dict[str, Any]:  # fmt: skip
    population = pl.read_parquet(config.detection_split_path).filter(pl.col("split") == split_name)
    driver = GraphDatabase.driver(config.neo4j_uri,
                                  auth=(config.neo4j_username, config.neo4j_password))  # fmt: skip
    with driver:
        edges = patterns.load_edges(driver, config.neo4j_database)
    roles, left_out = pattern_roles(config)
    total = degrees(edges)["total"]
    base_rate = population.filter(pl.col("seed_type") == "core").height / population.height
    report: dict[str, Any] = {
        "split": split_name,
        "population": {"seed_accounts": population.height,
                       "core": int((population["seed_type"] == "core").sum()),
                       "base_rate_core": round(base_rate, 4), "full_data": FULL_DATA,
                       "note": "The sample enriches laundering about 19x (ADR 005); sample "
                               "precision is not real-world precision."},
        "ground_truth": {"LAYERING": "IBM STACK", "MULE-HUB": "IBM GATHER-SCATTER",
                         "RAPID-MOVEMENT": "none (precision only)",
                         "patterns_left_out_no_role_account": left_out},
        "detectors": {},
    }  # fmt: skip
    tuning = chosen is None
    chosen = chosen or {}

    def measure(flagged: set[str], typology: str) -> dict[str, Any]:
        result = score(flagged, population, roles.get(typology, []))
        base = baseline(population, total, result["flagged_seed_accounts"])
        base_result = score(base, population, roles.get(typology, []))
        gain = round(result["precision"] - base_result["precision"], 4)
        return {**result, "precision_gain": gain, "_base": base, "_base_result": base_result}

    for typology in TUNED:
        entry: dict[str, Any] = {}
        if tuning:
            results = []
            for size, window in grid(typology, config):
                alerts = run_detector(edges, typology, size, window, config)
                flagged = set(alerts["accounts"].explode().drop_nulls())
                m = measure(flagged, typology)
                results.append(
                    {
                        "size": size,
                        "window_hours": window,
                        **{k: v for k, v in m.items() if not k.startswith("_")},
                    }
                )
            best = _pick(typology, results, config)
            chosen[typology] = [best["size"], best["window_hours"]]
            entry["grid"] = results
        size, window = int(chosen[typology][0]), float(chosen[typology][1])
        alerts = run_detector(edges, typology, size, window, config)
        flagged = set(alerts["accounts"].explode().drop_nulls())
        m = measure(flagged, typology)
        detector = {k: v for k, v in m.items() if not k.startswith("_")}
        if typology == "RAPID-MOVEMENT":
            detector = {
                k: v
                for k, v in detector.items()
                if k not in ("patterns", "patterns_caught", "pattern_recall", "f1")
            }
        entry.update({
            "size": size, "window_hours": window, "alerts_all_accounts": alerts.height,
            "flagged_accounts_all": len(flagged), "detector": detector,
            "degree_baseline": m["_base_result"],
            "detector_minus_baseline": paired_bootstrap(flagged, m["_base"], population,
                                                        roles.get(typology, []), config),
        })  # fmt: skip
        if typology == "RAPID-MOVEMENT":
            entry["detector_minus_baseline"].pop("recall_diff_ci95")
        report["detectors"][typology] = entry
    report["detectors"]["STRUCTURING"] = structuring_report(edges, population, config)
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
        dev_report = max(RESULTS_DIR.glob("detection_ext_dev_*.json"))
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
        path = RESULTS_DIR / f"detection_ext_dev_{stamp}.json"
    report["run_utc"] = stamp
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    summary = {t: {k: e.get(k) for k in ("size", "window_hours", "detector", "degree_baseline",
                                         "detector_minus_baseline")} if t in TUNED else e
               for t, e in report["detectors"].items()}  # fmt: skip
    print(json.dumps({"population": report["population"], "detectors": summary}, indent=2))
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
