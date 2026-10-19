"""ER evaluation (ADR 007): `make er-tune` (dev) and `make er-test` (test, once).

Reads ER outputs (`data/processed/er/`) and the eval-only ground truth and split. It reports:
- **Blocking:** pair completeness and reduction ratio, overall and per blocking key.
- **Decisions:** at the match threshold (`SAME_AS`) with the review band below it
  (`POSSIBLE_SAME_AS`, queued): precision, recall and F1, queue size, and the F1 if the queue were
  resolved perfectly.
- **Customer level:** ROC-AUC and AP (seeds vs plain, seeds vs look-alikes), plus hits@1 and hits@5.
- **Breakdowns:** recall by source, edit count, edit type, alias, customer type and seed reason;
  look-alike false-positive rate.
- **Baselines:** exact normalized name; rapidfuzz token-set ratio (threshold tuned on dev); Splink
  without embeddings; the full Splink model.
- **Leakage baseline:** the customer-only classifier from `leak_test.py`, trained on dev and scored
  on the same customers as ER. Also: ER vs leak AUC with bootstrap CI, leak + ER combined, and ER
  recall among seeds the leak classifier finds unremarkable.

Thresholds are tuned on dev only. `er-test` freezes every threshold from a dev report, requires
the configured ER thresholds to equal them, writes `eval/results/er_test_final.json`, and refuses
to run again.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from rapidfuzz import fuzz, process
from scipy import stats
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import StratifiedKFold

sys.path.insert(0, str(Path(__file__).parent))
from leak_test import Features, _boosting, load_customers  # noqa: E402

from tracegraph.config import Settings, settings  # noqa: E402
from tracegraph.er.blocking import build_records  # noqa: E402

VARIANTS = ("splink_no_embeddings", "splink_full")
RESULTS_DIR = Path(__file__).parent / "results"
TEST_RESULT = RESULTS_DIR / "er_test_final.json"


# --- inputs -----------------------------------------------------------------------------------


def load_inputs(config: Settings, split_name: str) -> dict[str, Any]:
    split = pl.read_parquet(config.er_split_path).filter(pl.col("split") == split_name)
    customers = set(split["customer_id"])
    truth = pl.read_parquet(config.seed_ground_truth_path).filter(
        pl.col("customer_id").is_in(list(customers))
    )
    edits = pl.read_parquet(config.customer_name_edits_path).select(
        "customer_id", pl.col("edits").list.len().alias("n_edits")
    )
    predictions = {
        v: pl.read_parquet(config.er_dir / f"predictions_{v}.parquet").filter(
            pl.col("customer_id").is_in(list(customers))
        )
        for v in VARIANTS
    }
    candidates = pl.read_parquet(config.er_dir / "candidates.parquet").filter(
        pl.col("customer_id").is_in(list(customers))
    )
    return {
        "split": split,
        "truth": truth,
        "positives": truth.filter(pl.col("is_match")).select(
            "customer_id", pl.col("source_entity_id").alias("entity_id")
        ),
        "edits": edits,
        "predictions": predictions,
        "candidates": candidates,
    }


# --- metrics ----------------------------------------------------------------------------------


def label_pairs(scored: pl.DataFrame, positives: pl.DataFrame) -> pl.DataFrame:
    return scored.join(
        positives.with_columns(pl.lit(True).alias("is_positive")),
        on=["customer_id", "entity_id"],
        how="left",
    ).with_columns(pl.col("is_positive").fill_null(False))


def decision_counts(
    scored: pl.DataFrame, positives: pl.DataFrame, threshold: float
) -> dict[str, int]:
    labelled = label_pairs(scored, positives)
    accepted = labelled.filter(pl.col("probability") >= threshold)
    tp = int(accepted["is_positive"].sum())
    return {"tp": tp, "fp": accepted.height - tp, "fn": positives.height - tp}


def prf(counts: dict[str, int]) -> dict[str, float]:
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def best_f1_threshold(scored: pl.DataFrame, positives: pl.DataFrame) -> tuple[float, float]:
    """(threshold, F1) maximizing pair-level F1 over observed scores."""
    labelled = label_pairs(scored, positives).sort("probability", descending=True)
    is_pos = labelled["is_positive"].to_numpy()
    probs = labelled["probability"].to_numpy()
    tp = np.cumsum(is_pos)
    fp = np.cumsum(~is_pos)
    fn = positives.height - tp
    f1 = 2 * tp / np.maximum(2 * tp + fp + fn, 1)
    # Only cut between distinct scores.
    last_of_value = np.r_[probs[1:] != probs[:-1], True]
    candidates = np.where(last_of_value)[0]
    best = candidates[np.argmax(f1[candidates])]
    return float(probs[best]), float(f1[best])


def review_threshold(
    scored: pl.DataFrame, positives: pl.DataFrame, target: float, match: float
) -> float:
    """Largest threshold ≤ match whose recall reaches `target` (else the lowest scored value)."""
    labelled = label_pairs(scored, positives).sort("probability", descending=True)
    recall = np.cumsum(labelled["is_positive"].to_numpy()) / max(positives.height, 1)
    probs = labelled["probability"].to_numpy()
    reach = np.where(recall >= target)[0]
    value = float(probs[reach[0]]) if reach.size else float(probs[-1])
    return min(value, match)


def customer_scores(scored: pl.DataFrame, customers: pl.DataFrame) -> pl.DataFrame:
    best = scored.group_by("customer_id").agg(pl.col("probability").max().alias("er_score"))
    return customers.join(best, on="customer_id", how="left").with_columns(
        pl.col("er_score").fill_null(0.0)
    )


def auc_ap(frame: pl.DataFrame, score: str, positive: str, negative: str) -> dict[str, float]:
    sub = frame.filter(pl.col("link_type").is_in([positive, negative]))
    y = (sub["link_type"] == positive).to_numpy()
    s = sub[score].to_numpy()
    return {
        "roc_auc": round(float(roc_auc_score(y, s)), 4),
        "ap": round(float(average_precision_score(y, s)), 4),
    }


def hits_at(
    scored: pl.DataFrame, positives: pl.DataFrame, ks: tuple[int, ...] = (1, 5)
) -> dict[str, float]:
    ranked = scored.with_columns(
        pl.col("probability").rank("ordinal", descending=True).over("customer_id").alias("rank")
    )
    ranks = positives.join(ranked, on=["customer_id", "entity_id"], how="left")["rank"]
    return {f"hits@{k}": round(float((ranks.fill_null(10**9) <= k).mean()), 4) for k in ks}


def bootstrap_ci(values: np.ndarray, statistic: Any, samples: int, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    n = len(values)
    stats_ = [statistic(values[rng.integers(0, n, n)]) for _ in range(samples)]
    return [
        round(float(np.percentile(stats_, 2.5)), 4),
        round(float(np.percentile(stats_, 97.5)), 4),
    ]


def f1_ci(
    scored: pl.DataFrame,
    positives: pl.DataFrame,
    customers: pl.DataFrame,
    threshold: float,
    config: Settings,
) -> list[float]:
    """Customer-cluster bootstrap CI for F1 at a fixed threshold."""
    labelled = label_pairs(scored, positives).filter(pl.col("probability") >= threshold)
    per = (
        customers.select("customer_id")
        .join(
            labelled.group_by("customer_id").agg(
                pl.col("is_positive").sum().alias("tp"), (~pl.col("is_positive")).sum().alias("fp")
            ),
            on="customer_id",
            how="left",
        )
        .join(
            positives.group_by("customer_id").len().rename({"len": "pos"}),
            on="customer_id",
            how="left",
        )
        .fill_null(0)
    )
    matrix = per.select("tp", "fp", "pos").to_numpy().astype(float)

    def f1(rows: np.ndarray) -> float:
        tp, fp, pos = rows.sum(axis=0)
        return 2 * tp / max(2 * tp + fp + (pos - tp), 1)

    return bootstrap_ci(matrix, f1, config.er_bootstrap_samples, config.random_seed)


# --- baselines --------------------------------------------------------------------------------


def string_baselines(candidates: pl.DataFrame, config: Settings) -> pl.DataFrame:
    """Exact normalized-name match and best rapidfuzz token-set ratio over candidate pairs."""
    customers, sources = build_records(config.staging_dir, config.er_block_max_token_frequency)
    names_c = dict(customers.select("unique_id", "name_norm").iter_rows())
    names_s = dict(sources.select("unique_id", "names").iter_rows())
    exact, ratio = [], []
    for customer_id, entity_id in candidates.select("customer_id", "entity_id").iter_rows():
        name, variants = names_c.get(customer_id, ""), names_s.get(entity_id, [])
        exact.append(1.0 if name in variants else 0.0)
        best = process.extractOne(name, variants, scorer=fuzz.token_set_ratio) if variants else None
        ratio.append((best[1] if best else 0.0) / 100)
    return candidates.select("customer_id", "entity_id").with_columns(
        pl.Series("exact", exact), pl.Series("token_set", ratio)
    )


# --- leakage baseline -------------------------------------------------------------------------


def leak_scores(config: Settings, split: pl.DataFrame, train_on: str) -> pl.DataFrame:
    """Customer-only GBM (seeds vs plain) trained on `train_on`, scored on every customer.

    For customers in the training split, scores are 5-fold out-of-fold.
    """
    customers = load_customers(config).join(split.select("customer_id", "split"), on="customer_id")
    train = customers.filter(pl.col("split") == train_on)
    labelled = train.filter(pl.col("link_type").is_in(["seeded_match", "plain"]))
    y = (labelled["link_type"] == "seeded_match").to_numpy().astype(int)
    oof = np.zeros(labelled.height)
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=config.random_seed).split(
        labelled, y
    ):
        features = Features()
        oof[te], _ = _boosting(
            features.fit_transform(labelled[tr]), features.transform(labelled[te]), y[tr]
        )
    features = Features()
    others = customers.filter(~pl.col("customer_id").is_in(labelled["customer_id"].implode()))
    scores_other, _ = _boosting(features.fit_transform(labelled), features.transform(others), y)
    return pl.concat(
        [
            labelled.select("customer_id").with_columns(pl.Series("leak_score", oof)),
            others.select("customer_id").with_columns(pl.Series("leak_score", scores_other)),
        ]
    )


def leakage_comparison(
    er_customers: pl.DataFrame,
    leak: pl.DataFrame,
    dev_er: pl.DataFrame,
    threshold: float,
    config: Settings,
    split_name: str,
) -> dict[str, Any]:
    frame = er_customers.join(leak, on="customer_id", how="left")
    result: dict[str, Any] = {}
    for positive, negative in (("seeded_match", "plain"), ("seeded_match", "hard_negative")):
        key = f"{positive}_vs_{negative}"
        sub = frame.filter(pl.col("link_type").is_in([positive, negative]))
        y = (sub["link_type"] == positive).to_numpy()
        er, lk = sub["er_score"].to_numpy(), sub["leak_score"].to_numpy()
        data = np.c_[y, er, lk]

        def diff(rows: np.ndarray) -> float:
            if rows[:, 0].min() == rows[:, 0].max():
                return 0.0
            return roc_auc_score(rows[:, 0], rows[:, 1]) - roc_auc_score(rows[:, 0], rows[:, 2])

        result[key] = {
            "er_roc_auc": round(float(roc_auc_score(y, er)), 4),
            "leak_roc_auc": round(float(roc_auc_score(y, lk)), 4),
            "er_ap": round(float(average_precision_score(y, er)), 4),
            "leak_ap": round(float(average_precision_score(y, lk)), 4),
            "auc_difference_ci95": bootstrap_ci(
                data, diff, config.er_bootstrap_samples, config.random_seed
            ),
        }

    # Added value: logistic [leak] vs [leak + ER], fitted on dev, evaluated on this split.
    dev = dev_er.join(leak, on="customer_id").filter(
        pl.col("link_type").is_in(["seeded_match", "plain"])
    )
    here = frame.filter(pl.col("link_type").is_in(["seeded_match", "plain"]))

    def design(f: pl.DataFrame, with_er: bool) -> np.ndarray:
        columns = [f["leak_score"].to_numpy()] + ([f["er_score"].to_numpy()] if with_er else [])
        return np.c_[tuple(columns)]

    y_dev = (dev["link_type"] == "seeded_match").to_numpy()
    y_here = (here["link_type"] == "seeded_match").to_numpy()
    small = LogisticRegression(max_iter=1000).fit(design(dev, False), y_dev)
    large = LogisticRegression(max_iter=1000).fit(design(dev, True), y_dev)

    def loglik(model: LogisticRegression, x: np.ndarray, y: np.ndarray) -> float:
        p = np.clip(model.predict_proba(x)[:, 1], 1e-12, 1 - 1e-12)
        return float(np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)))

    lr_stat = 2 * (
        loglik(large, design(dev, True), y_dev) - loglik(small, design(dev, False), y_dev)
    )
    result["added_value"] = {
        "combined_roc_auc": round(
            float(roc_auc_score(y_here, large.predict_proba(design(here, True))[:, 1])), 4
        ),
        "leak_only_roc_auc": round(
            float(roc_auc_score(y_here, small.predict_proba(design(here, False))[:, 1])), 4
        ),
        "likelihood_ratio_stat_dev": round(lr_stat, 2),
        "likelihood_ratio_p_dev": float(stats.chi2.sf(lr_stat, df=1)),
    }
    # ER recall among seeds the leak classifier ranks as unremarkable (below the plain median).
    plain_median = float(frame.filter(pl.col("link_type") == "plain")["leak_score"].median())
    quiet = frame.filter(
        (pl.col("link_type") == "seeded_match") & (pl.col("leak_score") < plain_median)
    )
    result["low_leak_score_seeds"] = {
        "n": quiet.height,
        "er_recall_at_match_threshold": round(float((quiet["er_score"] >= threshold).mean()), 4)
        if quiet.height
        else None,
    }
    result["split"] = split_name
    return result


# --- report -----------------------------------------------------------------------------------


def breakdowns(scored: pl.DataFrame, inputs: dict[str, Any], threshold: float) -> dict[str, Any]:
    truth, positives = inputs["truth"], inputs["positives"]
    accepted = label_pairs(scored, positives).filter(pl.col("probability") >= threshold)
    hit = (
        truth.filter(pl.col("is_match"))
        .join(
            accepted.filter(pl.col("is_positive"))
            .select("customer_id")
            .with_columns(pl.lit(True).alias("hit")),
            on="customer_id",
            how="left",
        )
        .with_columns(pl.col("hit").fill_null(False))
        .join(inputs["edits"], on="customer_id", how="left")
    )
    customer_type = pl.read_parquet(settings.staging_dir / "customers.parquet").select(
        "customer_id", "customer_type"
    )
    hit = hit.join(customer_type, on="customer_id", how="left").with_columns(
        pl.col("perturbations").list.contains("alias").alias("alias")
    )

    def by(column: str) -> dict[str, Any]:
        return {
            str(k): {"n": int(n), "recall": round(float(r), 4)}
            for k, n, r in hit.group_by(column)
            .agg(pl.len(), pl.col("hit").mean())
            .sort(column)
            .iter_rows()
        }

    edits = hit.explode("perturbations").filter(pl.col("perturbations").is_not_null())
    lookalikes = truth.filter(~pl.col("is_match"))
    imitated = lookalikes.select("customer_id", pl.col("source_entity_id").alias("entity_id")).join(
        accepted.select("customer_id", "entity_id"), on=["customer_id", "entity_id"], how="inner"
    )
    any_match = lookalikes.join(
        accepted.select("customer_id").unique(), on="customer_id", how="inner"
    )
    return {
        "recall_by_source": by("source_dataset"),
        "recall_by_edit_count": by("n_edits"),
        "recall_by_edit_type": {
            str(k): {"n": int(n), "recall": round(float(r), 4)}
            for k, n, r in edits.group_by("perturbations")
            .agg(pl.len(), pl.col("hit").mean())
            .sort("perturbations")
            .iter_rows()
        },
        "recall_by_alias": by("alias"),
        "recall_by_customer_type": by("customer_type"),
        "recall_by_seed_reason": by("seed_reason"),
        "lookalike_false_positive_rate": {
            "imitated_record_matched": round(imitated.height / max(lookalikes.height, 1), 4),
            "any_record_matched": round(any_match.height / max(lookalikes.height, 1), 4),
            "n_lookalikes": lookalikes.height,
        },
    }


def evaluate(
    config: Settings, split_name: str, thresholds: dict[str, float] | None
) -> dict[str, Any]:
    inputs = load_inputs(config, split_name)
    positives, candidates, split = inputs["positives"], inputs["candidates"], inputs["split"]
    report: dict[str, Any] = {
        "split": split_name,
        "n_customers": split.height,
        "n_positive_pairs": positives.height,
    }

    # Blocking
    blocked = positives.join(candidates, on=["customer_id", "entity_id"], how="left")
    report["blocking"] = {
        "pair_completeness": round(float(blocked["found_by"].is_not_null().mean()), 4),
        "pair_completeness_by_key": {
            key: round(float(blocked["found_by"].list.contains(key).fill_null(False).mean()), 4)
            for key in ("rare_tokens", "phon_keys", "ann_ids")
        },
        "candidate_pairs": candidates.height,
        "candidate_pairs_per_customer": round(candidates.height / max(split.height, 1), 1),
    }

    # Thresholds: every one is tuned on dev; test takes all of them from the dev report.
    full = inputs["predictions"]["splink_full"]
    tuning = thresholds is None
    if tuning:
        thresholds = {
            variant: round(best_f1_threshold(scored, positives)[0], 6)
            for variant, scored in inputs["predictions"].items()
        }
        thresholds["review"] = round(
            review_threshold(
                full, positives, config.er_review_recall_target, thresholds["splink_full"]
            ),
            6,
        )
    assert thresholds is not None
    report["thresholds"] = thresholds
    match, review = thresholds["splink_full"], thresholds["review"]

    report["models"] = {}
    for variant, scored in inputs["predictions"].items():
        variant_match = thresholds[variant]
        counts = decision_counts(scored, positives, variant_match)
        labelled = label_pairs(scored, positives)
        queue = labelled.filter(
            (pl.col("probability") >= review) & (pl.col("probability") < variant_match)
        )
        perfect = dict(counts)
        perfect["tp"] += int(queue["is_positive"].sum())
        perfect["fn"] -= int(queue["is_positive"].sum())
        entry = {
            "match_threshold": round(variant_match, 6),
            "counts": counts,
            **prf(counts),
            "f1_ci95": f1_ci(scored, positives, split, variant_match, config),
            "review_queue_pairs": queue.height,
            "review_queue_positives": int(queue["is_positive"].sum()),
            "f1_if_queue_resolved_perfectly": prf(perfect)["f1"],
            **hits_at(scored, positives),
        }
        er_customers = customer_scores(scored, split)
        entry["customer_level"] = {
            "seeds_vs_plain": auc_ap(er_customers, "er_score", "seeded_match", "plain"),
            "seeds_vs_lookalikes": auc_ap(
                er_customers, "er_score", "seeded_match", "hard_negative"
            ),
        }
        report["models"][variant] = entry
    report["breakdowns_full_model"] = breakdowns(full, inputs, match)

    # String baselines on the same candidate pairs (threshold tuned on dev).
    strings = string_baselines(candidates, config)
    for name in ("exact", "token_set"):
        scored = strings.select("customer_id", "entity_id", pl.col(name).alias("probability"))
        key = f"baseline_{name}"
        if tuning:
            thresholds[key] = (
                1.0 if name == "exact" else round(best_f1_threshold(scored, positives)[0], 6)
            )
        cut = thresholds[key]
        counts = decision_counts(scored, positives, cut)
        report["models"][f"baseline_{name}"] = {
            "match_threshold": round(cut, 4),
            "counts": counts,
            **prf(counts),
        }

    # Leakage baseline (customer-only classifier trained on dev).
    leak = leak_scores(config, pl.read_parquet(config.er_split_path), train_on="dev")
    dev_inputs = load_inputs(config, "dev") if split_name != "dev" else inputs
    dev_er = customer_scores(dev_inputs["predictions"]["splink_full"], dev_inputs["split"])
    report["leakage_baseline"] = leakage_comparison(
        customer_scores(full, split), leak, dev_er, match, config, split_name
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument(
        "--dev-report",
        type=Path,
        help="dev report whose thresholds are frozen for the test run (default: latest)",
    )
    args = parser.parse_args()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if args.split == "test":
        if TEST_RESULT.exists():
            print(
                f"{TEST_RESULT} exists: the test split has already been scored. Refusing to re-run."
            )
            return 2
        dev_report = args.dev_report or max(RESULTS_DIR.glob("er_dev_*.json"))
        thresholds = json.loads(dev_report.read_text())["thresholds"]
        if (thresholds["splink_full"], thresholds["review"]) != (
            settings.er_match_threshold,
            settings.er_review_threshold,
        ):
            print(f"Config thresholds differ from {dev_report.name}; set them from dev first.")
            return 2
        report = evaluate(settings, "test", thresholds)
        report["thresholds_from"] = dev_report.name
        path = TEST_RESULT
    else:
        report = evaluate(settings, "dev", None)
        path = RESULTS_DIR / f"er_dev_{stamp}.json"
    report["run_utc"] = stamp
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
