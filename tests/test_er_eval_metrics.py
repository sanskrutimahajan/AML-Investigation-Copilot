"""ER evaluation metric helpers on hand-computed examples."""

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parents[1] / "eval"))
import er_eval  # noqa: E402

SCORED = pl.DataFrame(
    {
        "customer_id": ["a", "a", "b", "c", "d"],
        "entity_id": ["x", "y", "z", "w", "v"],
        "probability": [0.95, 0.40, 0.90, 0.80, 0.30],
    }
)
POSITIVES = pl.DataFrame({"customer_id": ["a", "b", "e"], "entity_id": ["x", "z", "q"]})


def test_decision_counts_include_unblocked_positives_as_misses() -> None:
    counts = er_eval.decision_counts(SCORED, POSITIVES, 0.85)
    assert counts == {"tp": 2, "fp": 0, "fn": 1}  # (e, q) was never a candidate
    assert er_eval.prf(counts) == {"precision": 1.0, "recall": 0.6667, "f1": 0.8}


def test_best_threshold_and_review_band() -> None:
    threshold, f1 = er_eval.best_f1_threshold(SCORED, POSITIVES)
    assert threshold == 0.90 and round(f1, 4) == 0.8
    # Below 0.90 the queue holds 0.80, 0.40, 0.30 (in that order).
    assert er_eval.review_threshold(SCORED, threshold, cap=2) == 0.40
    assert er_eval.review_threshold(SCORED, threshold, cap=10) == 0.30
    assert er_eval.review_threshold(SCORED, threshold, cap=0) == threshold


def test_queue_recall_gain() -> None:
    gain = er_eval.queue_recall_gain(SCORED, POSITIVES, 0.85, (1, 3))
    assert gain == {"queue_1": 0.0, "queue_3": 0.0}  # no positive sits below the threshold
    gain = er_eval.queue_recall_gain(SCORED, POSITIVES, 0.92, (1, 2))
    assert gain == {"queue_1": round(1 / 3, 4), "queue_2": round(1 / 3, 4)}


def test_hits_at_k() -> None:
    hits = er_eval.hits_at(SCORED, POSITIVES)
    assert hits == {"hits@1": round(2 / 3, 4), "hits@5": round(2 / 3, 4)}


def test_paired_f1_difference_ci() -> None:
    customers = pl.DataFrame({"customer_id": ["e", "d", "c", "b", "a"]})
    config = er_eval.Settings(_env_file=None, er_bootstrap_samples=200)
    counts = er_eval._customer_counts(SCORED, POSITIVES, customers, 0.85)
    assert counts.tolist() == [[1, 0, 1], [1, 0, 1], [0, 0, 0], [0, 0, 0], [0, 0, 1]]  # a..e
    assert er_eval._f1(counts) == 0.8
    same = er_eval.f1_difference_ci((SCORED, 0.85), (SCORED, 0.85), POSITIVES, customers, config)
    assert same == [0.0, 0.0]
    # At 0.92 only (a, x) is accepted: F1 0.5, so the full-sample difference is -0.3.
    low, high = er_eval.f1_difference_ci(
        (SCORED, 0.85), (SCORED, 0.92), POSITIVES, customers, config
    )
    assert low <= -0.3 <= high <= 0.0
