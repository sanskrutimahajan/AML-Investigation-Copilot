"""Detection evaluation helpers on hand-computed examples."""

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parents[1] / "eval"))
import detection_eval as de  # noqa: E402
import detection_split as ds  # noqa: E402

from tracegraph.config import Settings  # noqa: E402

POPULATION = pl.DataFrame(
    {
        "account_id": ["hub", "m1", "m2", "b1", "b2"],
        "seed_type": ["core", "core", "core", "benign", "benign"],
        "group": ["g1", "g1", "g1", "b1", "b2"],
    }
)
ROLES = [{"hub"}, {"m2"}]  # two patterns


def test_score_counts_precision_on_seeds_and_pattern_recall() -> None:
    result = de.score({"hub", "b1", "outsider"}, POPULATION, ROLES)
    assert result["flagged_seed_accounts"] == 2  # the non-seed outsider is excluded
    assert result["precision"] == 0.5 and result["pattern_recall"] == 0.5
    assert result["patterns"] == 2 and result["patterns_caught"] == 1


def test_degree_baseline_flags_the_top_n_with_id_tie_break() -> None:
    degree = {"hub": 9, "b1": 9, "m1": 3}
    assert de.baseline(POPULATION, degree, 2) == {"b1", "hub"}
    assert de.baseline(POPULATION, degree, 3) == {"b1", "hub", "m1"}


def test_identical_flags_give_a_zero_difference_interval() -> None:
    config = Settings(_env_file=None, detect_bootstrap_samples=200)
    same = de.paired_bootstrap({"hub"}, {"hub"}, POPULATION, ROLES, config)
    assert same == {"precision_diff_ci95": [0.0, 0.0], "recall_diff_ci95": [0.0, 0.0]}


def test_components_join_accounts_that_share_transactions() -> None:
    groups = ds._components([("a", "b"), ("b", "c"), ("x", "y")])
    assert groups["a"] == groups["b"] == groups["c"] != groups["x"] == groups["y"]
