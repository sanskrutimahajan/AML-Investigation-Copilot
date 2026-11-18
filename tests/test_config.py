"""Configuration validation tests."""

import pytest
from pydantic import ValidationError

from tracegraph.config import Settings


def test_settings_have_documented_defaults() -> None:
    config = Settings(_env_file=None)
    assert config.er_match_threshold == 0.999779
    assert config.er_review_threshold == 0.991478
    assert config.seeded_link_rate == 0.03


def test_review_threshold_cannot_exceed_match_threshold() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, er_review_threshold=0.95, er_match_threshold=0.92)


def test_er_decision_variant_selects_its_predictions_file() -> None:
    default = Settings(_env_file=None)
    assert default.er_decision_variant == "splink_no_embeddings"
    assert default.er_decision_predictions_path.name == "predictions_splink_no_embeddings.parquet"
    full = Settings(_env_file=None, er_decision_variant="splink_full")
    assert full.er_decision_predictions_path.name == "predictions_splink_full.parquet"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, er_decision_variant="splink_other")
