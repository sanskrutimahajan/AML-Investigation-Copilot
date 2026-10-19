"""Unsupervised Splink linkage model: customers ↔ source entities (ADR 007).

Every comparison is symmetric, because Splink link-only mode may put either table on the left.
- **name:** the best Jaro-Winkler score over every pair of name variants (source aliases included).
- **tokens:** Jaccard overlap of the name-token sets (legal suffixes removed).
- **embedding:** cosine similarity of the primary-name embeddings (left out in the no-embedding
  variant).
- **suffix:** same legal-suffix class.
- **dob:** exact date or same year.
- **country:** exact match.

Training uses no labels: `u` comes from random sampling, `m` from EM on plain equality blocks, and
the prior from a deterministic rule with an assumed recall (`er_deterministic_recall`). Ground truth
is never read here.
"""

import logging
from typing import Any

import polars as pl
import splink.comparison_library as cl
from splink import Linker, SettingsCreator, block_on
from splink.backends.duckdb import DuckDBAPI
from splink.internals.exceptions import EMTrainingException

from tracegraph.config import Settings

logger = logging.getLogger(__name__)

_BEST_JW = (
    "list_max(list_transform(names_l, lambda a: list_max(list_transform(names_r, "
    "lambda b: jaro_winkler_similarity(a, b)))))"
)
_JACCARD = (
    "len(list_intersect(tokens_l, tokens_r)) / "
    "greatest(len(list_distinct(list_concat(tokens_l, tokens_r))), 1)"
)


def _levels(expression: str, thresholds: list[float], label: str) -> list[dict[str, Any]]:
    return [
        {"sql_condition": f"{expression} >= {t}", "label_for_charts": f"{label} >= {t}"}
        for t in thresholds
    ] + [{"sql_condition": "ELSE", "label_for_charts": "All other"}]


def comparisons(with_embeddings: bool) -> list[Any]:
    name = cl.CustomComparison(
        output_column_name="name",
        comparison_levels=[
            {"sql_condition": "names_l IS NULL OR names_r IS NULL", "is_null_level": True},
            *_levels(_BEST_JW, [0.98, 0.94, 0.88, 0.80], "best JW"),
        ],
        comparison_description="Best Jaro-Winkler across name variants",
    )
    tokens = cl.CustomComparison(
        output_column_name="tokens",
        comparison_levels=[
            {
                "sql_condition": "len(tokens_l) = 0 OR len(tokens_r) = 0",
                "is_null_level": True,
            },
            *_levels(_JACCARD, [0.99, 0.6, 0.3], "token Jaccard"),
        ],
        comparison_description="Token-set Jaccard",
    )
    dob = cl.CustomComparison(
        output_column_name="dob",
        comparison_levels=[
            {"sql_condition": "dob_year_l IS NULL OR dob_year_r IS NULL", "is_null_level": True},
            {"sql_condition": "dob_l = dob_r", "label_for_charts": "Exact date"},
            {"sql_condition": "dob_year_l = dob_year_r", "label_for_charts": "Same year"},
            {"sql_condition": "ELSE", "label_for_charts": "All other"},
        ],
        comparison_description="Date of birth",
    )
    result = [
        name,
        tokens,
        cl.ExactMatch("suffix_class"),
        dob,
        cl.ExactMatch("country"),
    ]
    if with_embeddings:
        result.insert(
            2,
            cl.CustomComparison(
                output_column_name="embedding",
                comparison_levels=[
                    {
                        "sql_condition": "embedding_l IS NULL OR embedding_r IS NULL",
                        "is_null_level": True,
                    },
                    *_levels(
                        "list_cosine_similarity(embedding_l, embedding_r)",
                        [0.95, 0.88, 0.80],
                        "cosine",
                    ),
                ],
                comparison_description="Name embedding cosine",
            ),
        )
    return result


PREDICTION_BLOCKING_RULES: list[Any] = [
    block_on("kind", "rare_tokens", arrays_to_explode=["rare_tokens"]),
    block_on("kind", "phon_keys", arrays_to_explode=["phon_keys"]),
    block_on("kind", "ann_ids", arrays_to_explode=["ann_ids"]),
]
MODEL_COLUMNS = [
    "unique_id",
    "kind",
    "name_norm",
    "names",
    "tokens",
    "suffix_class",
    "dob",
    "dob_year",
    "country",
    "rare_tokens",
    "phon_keys",
    "ann_ids",
    "first_block_key",
]


def train_and_predict(
    customers: pl.DataFrame,
    sources: pl.DataFrame,
    config: Settings,
    *,
    with_embeddings: bool,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Return scored candidate pairs (customer_id, entity_id, probability, ...) and the model."""
    columns = [*MODEL_COLUMNS, *(["embedding"] if with_embeddings else [])]
    db = DuckDBAPI()
    left = db.register(customers.select(columns).to_arrow(), dataset_display_name="customers")
    right = db.register(sources.select(columns).to_arrow(), dataset_display_name="sources")
    settings = SettingsCreator(
        link_type="link_only",
        comparisons=comparisons(with_embeddings),
        blocking_rules_to_generate_predictions=PREDICTION_BLOCKING_RULES,
        retain_intermediate_calculation_columns=True,
        additional_columns_to_retain=["kind"],
    )
    linker = Linker([left, right], settings, log_level=30)
    linker.training.estimate_probability_two_random_records_match(
        [block_on("kind", "name_norm")], recall=config.er_deterministic_recall
    )
    linker.training.estimate_u_using_random_sampling(
        max_pairs=config.er_u_max_pairs, seed=config.random_seed
    )
    # EM on plain equality blocks (Splink cannot run EM on exploded rules); each session
    # estimates m for every comparison not used in its own blocking rule.
    em_sessions: list[str] = []
    for rule in (("kind", "first_block_key"), ("kind", "dob")):
        try:
            linker.training.estimate_parameters_using_expectation_maximisation(block_on(*rule))
            em_sessions.append("+".join(rule))
        except EMTrainingException:
            logger.warning("EM session on %s produced no pairs; skipped", rule)
    if not em_sessions:
        raise RuntimeError("no EM session produced training pairs; m values are untrained")

    predictions = pl.from_arrow(
        linker.inference.predict(
            threshold_match_probability=config.er_prediction_floor
        ).as_pyarrow_table()
    )
    assert isinstance(predictions, pl.DataFrame)
    customer_on_left = pl.col("source_dataset_l") == "customers"
    gamma = [c for c in predictions.columns if c.startswith("gamma_")]
    scored = predictions.select(
        pl.when(customer_on_left)
        .then(pl.col("unique_id_l"))
        .otherwise(pl.col("unique_id_r"))
        .alias("customer_id"),
        pl.when(customer_on_left)
        .then(pl.col("unique_id_r"))
        .otherwise(pl.col("unique_id_l"))
        .alias("entity_id"),
        pl.col("match_probability").alias("probability"),
        pl.col("match_weight"),
        *gamma,
    ).sort("customer_id", "probability", descending=[False, True])
    model = linker.misc.save_model_to_json()
    model["em_sessions"] = em_sessions
    return scored, model
