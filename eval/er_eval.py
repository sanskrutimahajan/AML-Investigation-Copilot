"""ER evaluation (ADR 007): `make er-tune` (dev) and `make er-test` (test, once).

Reads ER outputs (`data/processed/er/`) and the eval-only ground truth and split. It reports:
- **Blocking:** pair completeness and reduction ratio, overall and per blocking key.
- **Decisions:** at the match threshold (`SAME_AS`) with the review band below it
  (`POSSIBLE_SAME_AS`, queued): precision, recall and F1, queue size, and the F1 if the queue were
  resolved perfectly.
- **Customer level:** ROC-AUC and AP (seeds vs plain, seeds vs look-alikes), plus hits@1 and hits@5.
- **Breakdowns:** recall by source, edit count, edit type, alias, customer type and seed reason;
  look-alike false-positive rate.
- **Models:** both Splink variants (without embeddings, full) are reported. Decisions, the review
  band, breakdowns and the leakage comparison use `er_decision_variant` from config.
- **Baselines:** exact normalized name; rapidfuzz token-set ratio (threshold tuned on dev).
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
from tracegraph.er.adjudicator import (  # noqa: E402
    ADJUDICATIONS_FILE,
    final_edges,
    load_adjudications,
    review_band,
)
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


def review_threshold(scored: pl.DataFrame, match: float, cap: int) -> float:
    """Lowest threshold whose band [threshold, match) holds at most `cap` pairs."""
    below = scored.filter(pl.col("probability") < match).sort("probability", descending=True)
    if below.height == 0:
        return match
    if below.height <= cap:
        return float(below["probability"].min())  # type: ignore[arg-type]
    cut = float(below["probability"][cap])  # the (cap+1)-th pair is excluded
    band = below.filter(pl.col("probability") > cut)
    return float(band["probability"].min()) if band.height else match  # type: ignore[arg-type]


def queue_recall_gain(
    scored: pl.DataFrame, positives: pl.DataFrame, match: float, sizes: tuple[int, ...]
) -> dict[str, float]:
    """Recall added if the top-N queued pairs (below the match threshold) were all resolved."""
    below = label_pairs(scored, positives).filter(pl.col("probability") < match)
    below = below.sort("probability", descending=True)
    positives_found = np.cumsum(below["is_positive"].to_numpy())
    total = max(positives.height, 1)
    return {
        f"queue_{n}": round(float(positives_found[min(n, len(positives_found)) - 1]) / total, 4)
        if len(positives_found)
        else 0.0
        for n in sizes
    }


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


def _customer_counts(
    scored: pl.DataFrame, positives: pl.DataFrame, customers: pl.DataFrame, threshold: float
) -> np.ndarray:
    """Per-customer (tp, fp, positives) at a threshold, one row per customer in id order."""
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
        .sort("customer_id")
    )
    return per.select("tp", "fp", "pos").to_numpy().astype(float)


def _f1(rows: np.ndarray) -> float:
    tp, fp, pos = rows.sum(axis=0)
    return float(2 * tp / max(2 * tp + fp + (pos - tp), 1))


def f1_ci(
    scored: pl.DataFrame,
    positives: pl.DataFrame,
    customers: pl.DataFrame,
    threshold: float,
    config: Settings,
) -> list[float]:
    """Customer-cluster bootstrap CI for F1 at a fixed threshold."""
    matrix = _customer_counts(scored, positives, customers, threshold)
    return bootstrap_ci(matrix, _f1, config.er_bootstrap_samples, config.random_seed)


def f1_difference_ci(
    base: tuple[pl.DataFrame, float],
    other: tuple[pl.DataFrame, float],
    positives: pl.DataFrame,
    customers: pl.DataFrame,
    config: Settings,
) -> list[float]:
    """Paired customer-cluster bootstrap CI for F1(other) - F1(base), each (scored, threshold)."""
    matrix = np.hstack(
        [_customer_counts(scored, positives, customers, cut) for scored, cut in (base, other)]
    )
    return bootstrap_ci(
        matrix,
        lambda rows: _f1(rows[:, 3:]) - _f1(rows[:, :3]),
        config.er_bootstrap_samples,
        config.random_seed,
    )


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


def leak_scores(
    config: Settings, split: pl.DataFrame, train_on: str, positive: str = "seeded_match"
) -> pl.DataFrame:
    """Customer-only GBM (`positive` vs plain) trained on `train_on`, scored on every customer.

    For customers in the training split, scores are 5-fold out-of-fold.
    """
    customers = load_customers(config).join(split.select("customer_id", "split"), on="customer_id")
    train = customers.filter(pl.col("split") == train_on)
    labelled = train.filter(pl.col("link_type").is_in([positive, "plain"]))
    y = (labelled["link_type"] == positive).to_numpy().astype(int)
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


def homonym_name_style_check(
    scored: pl.DataFrame,
    truth: pl.DataFrame,
    split_customers: pl.DataFrame,
    homonym_leak: pl.DataFrame,
    threshold: float,
) -> dict[str, Any]:
    """Are ER's homonym false positives explained by name style alone (ADR 007)?

    The homonym leak score comes from a customer-only classifier (homonym vs plain). If ER's
    errors on homonyms were driven by name style, the false-positive rate would rise with that score
    and the score would predict which homonyms ER wrongly matches (AUC well above 0.5).
    """
    homonyms = truth.filter(pl.col("link_type") == "homonym").select(
        "customer_id", pl.col("source_entity_id").alias("entity_id")
    )
    pair = homonyms.join(scored, on=["customer_id", "entity_id"], how="left").with_columns(
        (pl.col("probability").fill_null(0) >= threshold).alias("false_positive")
    )
    frame = pair.join(homonym_leak, on="customer_id", how="left")
    er_customers = customer_scores(scored, split_customers).join(homonym_leak, on="customer_id")
    sub = er_customers.filter(pl.col("link_type").is_in(["homonym", "plain"]))
    y = (sub["link_type"] == "homonym").to_numpy()
    tertiles = frame.with_columns(
        pl.col("leak_score")
        .qcut(3, labels=["low", "mid", "high"], allow_duplicates=True)
        .alias("t")
    )
    fp = frame["false_positive"].to_numpy()
    return {
        "n_homonyms": frame.height,
        "false_positive_rate": round(float(fp.mean()), 4) if frame.height else None,
        "fp_rate_by_homonym_leak_tertile": {
            str(t): round(float(g["false_positive"].mean()), 4)
            for (t,), g in tertiles.group_by("t")
        },
        "leak_score_auc_for_predicting_fp": round(
            float(roc_auc_score(fp, frame["leak_score"].to_numpy())), 4
        )
        if 0 < fp.sum() < len(fp)
        else None,
        "homonym_leak_roc_auc_vs_plain": round(
            float(roc_auc_score(y, sub["leak_score"].to_numpy())), 4
        ),
        "er_roc_auc_homonym_vs_plain": round(
            float(roc_auc_score(y, sub["er_score"].to_numpy())), 4
        ),
    }


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
    for positive, negative in (
        ("seeded_match", "plain"),
        ("seeded_match", "hard_negative"),
        ("seeded_match", "homonym"),
    ):
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

    def logit(values: np.ndarray) -> np.ndarray:
        p = np.clip(values, 1e-9, 1 - 1e-9)
        return np.log(p / (1 - p))

    def design(f: pl.DataFrame, with_er: bool) -> np.ndarray:
        columns = [logit(f["leak_score"].to_numpy())]
        if with_er:
            columns.append(logit(f["er_score"].to_numpy()))
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
    lookalikes = truth.filter(pl.col("link_type") == "hard_negative")
    homonyms = truth.filter(pl.col("link_type") == "homonym")
    homonym_pairs = homonyms.select("customer_id", pl.col("source_entity_id").alias("entity_id"))
    homonym_hits = homonym_pairs.join(
        accepted.select("customer_id", "entity_id"), on=["customer_id", "entity_id"], how="inner"
    )
    queued = scored.filter(pl.col("probability") < threshold).join(
        homonym_pairs, on=["customer_id", "entity_id"], how="inner"
    )
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
        "homonyms": {
            "n_homonyms": homonyms.height,
            "false_positive_rate_same_as": round(homonym_hits.height / max(homonyms.height, 1), 4),
            "by_conflict": {
                str(k): {
                    "n": int(n),
                    "false_positive_rate": round(
                        homonym_hits.join(g.select("customer_id"), on="customer_id").height
                        / max(int(n), 1),
                        4,
                    ),
                }
                for (k,), g in homonyms.with_columns(
                    pl.col("perturbations").list.first().alias("conflict")
                ).group_by("conflict")
                for n in [g.height]
            },
            "scored_below_match": queued.height,
            "share_of_all_false_positives": round(
                homonym_hits.height / max(accepted.filter(~pl.col("is_positive")).height, 1),
                4,
            ),
        },
        "lookalike_false_positive_rate": {
            "imitated_record_matched": round(imitated.height / max(lookalikes.height, 1), 4),
            "any_record_matched": round(any_match.height / max(lookalikes.height, 1), 4),
            "n_lookalikes": lookalikes.height,
        },
    }


def llm_adjudication(
    full: pl.DataFrame,
    inputs: dict[str, Any],
    match: float,
    review: float,
    adjudications: pl.DataFrame,
    homonym_leak: pl.DataFrame,
    config: Settings,
) -> dict[str, Any]:
    """Step B on this split: decisions vs truth, F1 with and without the LLM, failure counts."""
    made_with = adjudications.select("match_threshold", "review_threshold").unique().rows()
    if made_with != [(match, review)]:
        return {"skipped": f"adjudications made with thresholds {made_with}, not {(match, review)}"}
    positives, split = inputs["positives"], inputs["split"]
    band = label_pairs(review_band(full, match, review), positives).join(
        adjudications.select("customer_id", "entity_id", "decision", "attempts", "latency_seconds"),
        on=["customer_id", "entity_id"],
        how="left",
    )
    edges = final_edges(full, match, review, adjudications)
    # Final decisions as a 0/1 score, so the Step A metric functions apply unchanged at cut 1.0.
    as_scored = edges.select(
        "customer_id",
        "entity_id",
        (pl.col("edge") == "SAME_AS").cast(pl.Float64).alias("probability"),
    )
    counts = decision_counts(as_scored, positives, 1.0)
    without = decision_counts(full, positives, match)
    after = breakdowns(as_scored, inputs, 1.0)
    run = adjudications.select("provider", "model", "prompt_version").unique().to_dicts()
    latency = band["latency_seconds"].drop_nulls()
    return {
        "llm": run,
        "band_pairs": band.height,
        "band_pairs_not_adjudicated": int(band["decision"].is_null().sum()),
        "decisions_by_truth": {
            str(decision): {"positive": int(pos), "negative": int(n - pos)}
            for decision, n, pos in band.group_by("decision")
            .agg(pl.len(), pl.col("is_positive").sum())
            .sort("decision")
            .iter_rows()
        },
        "json_retries": int((band["attempts"] > 1).sum()),
        "failed": int((band["decision"] == "failed").sum()),
        "mean_latency_seconds": (
            round(float(latency.mean()), 3) if latency.len() else None  # type: ignore[arg-type]
        ),
        "without_llm": {"counts": without, **prf(without)},
        "with_llm": {
            "counts": counts,
            **prf(counts),
            "f1_ci95": f1_ci(as_scored, positives, split, 1.0, config),
            "still_queued": int((edges["edge"] == "POSSIBLE_SAME_AS").sum()),
        },
        "f1_gain_ci95": f1_difference_ci((full, match), (as_scored, 1.0), positives, split, config),
        "homonyms": after["homonyms"],
        "lookalike_false_positive_rate": after["lookalike_false_positive_rate"],
        "homonym_name_style_check": homonym_name_style_check(
            as_scored, inputs["truth"], split, homonym_leak, 1.0
        ),
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
    variant_name = config.er_decision_variant
    full = inputs["predictions"][variant_name]
    tuning = thresholds is None
    if tuning:
        thresholds = {
            variant: round(best_f1_threshold(scored, positives)[0], 6)
            for variant, scored in inputs["predictions"].items()
        }
        thresholds["review"] = round(
            review_threshold(full, thresholds[variant_name], config.er_review_queue_cap), 6
        )
    assert thresholds is not None
    report["decision_variant"] = variant_name
    # Whether the written edges include Step B's LLM decisions (else Splink alone decides).
    report["llm_decides_edges"] = config.er_use_llm_adjudication
    report["thresholds"] = thresholds
    match, review = thresholds[variant_name], thresholds["review"]

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
            "recall_at_match": prf(counts)["recall"],
            "recall_gain_by_queue_size": queue_recall_gain(
                scored, positives, variant_match, (100, 250, 500, 1000)
            ),
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
    report["breakdowns_decision_model"] = breakdowns(full, inputs, match)

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
    dev_er = customer_scores(dev_inputs["predictions"][variant_name], dev_inputs["split"])
    report["leakage_baseline"] = leakage_comparison(
        customer_scores(full, split), leak, dev_er, match, config, split_name
    )
    homonym_leak = leak_scores(
        config, pl.read_parquet(config.er_split_path), train_on="dev", positive="homonym"
    )
    report["homonym_name_style_check"] = homonym_name_style_check(
        full, inputs["truth"], split, homonym_leak, match
    )
    path = config.er_dir / ADJUDICATIONS_FILE
    report["llm_adjudication"] = (
        llm_adjudication(full, inputs, match, review, pl.read_parquet(path), homonym_leak, config)
        if path.exists()
        else {"skipped": "Step B has not run (make er-adjudicate)"}
    )
    llm_runs = report["llm_adjudication"].get("llm") or []
    if len(llm_runs) == 1:  # every eval file records the LLM that produced it (CLAUDE.md)
        report["llm"] = llm_runs[0]
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
        dev = json.loads(dev_report.read_text())
        thresholds = dev["thresholds"]
        variant = settings.er_decision_variant
        if dev.get("decision_variant") != variant:
            print(f"{dev_report.name} was tuned for {dev.get('decision_variant')}, not {variant}.")
            return 2
        if (thresholds[variant], thresholds["review"]) != (
            settings.er_match_threshold,
            settings.er_review_threshold,
        ):
            print(f"Config thresholds differ from {dev_report.name}; set them from dev first.")
            return 2
        # The test split is scored once, so Step B must already cover the whole review band.
        band = review_band(
            pl.read_parquet(settings.er_decision_predictions_path),
            settings.er_match_threshold,
            settings.er_review_threshold,
        )
        adjudications = load_adjudications(settings)
        missing = (
            band.height
            if adjudications is None
            else band.join(adjudications, on=["customer_id", "entity_id"], how="anti").height
        )
        if missing:
            print(f"{missing} review-band pairs have no LLM decision; run `make er-adjudicate`.")
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
