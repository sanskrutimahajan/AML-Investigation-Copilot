"""LLM provider construction tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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


def test_openai_compat_allows_keyless_local_endpoint() -> None:
    response = MagicMock()
    response.choices[0].message.content = "ok"
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    provider = OpenAICompatProvider(Settings(_env_file=None, llm_enabled=True))

    with patch(
        "tracegraph.llm.openai_compat_provider.AsyncOpenAI", return_value=client
    ) as client_cls:
        assert asyncio.run(provider.generate("hi")) == "ok"

    assert client_cls.call_args.kwargs["api_key"] == "EMPTY"


def test_anthropic_requires_key_when_enabled() -> None:
    provider = AnthropicProvider(Settings(_env_file=None, llm_enabled=True))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is required"):
        asyncio.run(provider.generate("No network call should be made."))
