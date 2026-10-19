"""Configuration validation tests."""

import pytest
from pydantic import ValidationError

from tracegraph.config import Settings


def test_settings_have_documented_defaults() -> None:
    config = Settings(_env_file=None)
    assert config.er_match_threshold == 0.92
    assert config.seeded_link_rate == 0.03


def test_review_threshold_cannot_exceed_match_threshold() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, er_review_threshold=0.95, er_match_threshold=0.92)
