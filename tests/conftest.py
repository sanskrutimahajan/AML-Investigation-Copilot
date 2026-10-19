"""Shared fixtures that keep tests independent of a developer's local environment."""

import pytest

from tracegraph.config import Settings


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop exported settings variables so tests see code defaults only."""
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
