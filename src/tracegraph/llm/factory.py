"""Build the LLM provider from config only (ADR 008)."""

from tracegraph.config import Settings
from tracegraph.llm.anthropic_provider import AnthropicProvider
from tracegraph.llm.base import LLMProvider, LLMSettings
from tracegraph.llm.openai_compat_provider import OpenAICompatProvider


def llm_settings(config: Settings) -> LLMSettings:
    settings = LLMSettings(
        provider=config.llm_provider,
        base_url=config.llm_base_url,
        api_key=config.llm_api_key,
        model=config.llm_model,
        max_tokens=config.llm_max_tokens,
        temperature=config.llm_temperature,
        timeout_seconds=config.llm_timeout_seconds,
    )
    if settings.provider == "openai_compatible" and not settings.base_url:
        raise ValueError("LLM_PROVIDER=openai_compatible requires LLM_BASE_URL")
    return settings


def get_provider(config: Settings) -> LLMProvider:
    settings = llm_settings(config)
    if settings.provider == "anthropic":
        return AnthropicProvider(settings)
    # Ollama and every OpenAI-compatible server share one implementation.
    return OpenAICompatProvider(settings)
