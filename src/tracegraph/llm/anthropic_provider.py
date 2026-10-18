"""Anthropic Messages API provider."""

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

from tracegraph.config import Settings, settings


class AnthropicProvider:
    """Generate text with the configured Anthropic model."""

    def __init__(self, config: Settings = settings) -> None:
        self._model = config.anthropic_model
        self._enabled = config.llm_enabled
        self._api_key = config.anthropic_api_key
        self._client: AsyncAnthropic | None = None

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Generate text only when explicitly enabled with a configured API key."""
        if not self._enabled:
            raise RuntimeError(
                "LLM calls are disabled; explicit user approval is required to enable."
            )
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when LLM calls are enabled.")
        if self._client is None:
            self._client = AsyncAnthropic(api_key=self._api_key)

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if isinstance(block, TextBlock))
