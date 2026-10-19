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
    # Recall can never pass 2/3 here (one positive is unblocked): fall back to the lowest score.
    assert er_eval.review_threshold(SCORED, POSITIVES, 0.95, threshold) == 0.30
    assert er_eval.review_threshold(SCORED, POSITIVES, 0.6, threshold) == 0.90


def test_hits_at_k() -> None:
    hits = er_eval.hits_at(SCORED, POSITIVES)
    assert hits == {"hits@1": round(2 / 3, 4), "hits@5": round(2 / 3, 4)}
