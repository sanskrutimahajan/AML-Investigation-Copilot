"""LLM provider construction tests."""

import asyncio

import pytest

from tracegraph.config import Settings
from tracegraph.llm.anthropic_provider import AnthropicProvider
from tracegraph.llm.base import LLMProvider
from tracegraph.llm.openai_compat_provider import OpenAICompatProvider


def test_providers_use_models_from_config_without_creating_clients() -> None:
    config = Settings(
        _env_file=None,
        anthropic_model="configured-anthropic-model",
        openai_compat_model="configured-openai-model",
    )
    anthropic = AnthropicProvider(config)
    openai_compat = OpenAICompatProvider(config)

    assert anthropic._model == "configured-anthropic-model"
    assert openai_compat._model == "configured-openai-model"
    assert anthropic._client is None
    assert openai_compat._client is None


@pytest.mark.parametrize(
    "provider",
    [
        AnthropicProvider(Settings(_env_file=None, llm_enabled=False)),
        OpenAICompatProvider(Settings(_env_file=None, llm_enabled=False)),
    ],
)
def test_provider_calls_are_disabled_by_default(provider: LLMProvider) -> None:
    with pytest.raises(RuntimeError, match="LLM calls are disabled"):
        asyncio.run(provider.generate("No network call should be made."))
