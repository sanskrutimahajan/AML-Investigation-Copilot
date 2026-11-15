"""Anthropic Messages API provider (used only when LLM_PROVIDER=anthropic with an API key)."""

import time
from typing import Any

from anthropic import Anthropic, omit
from anthropic.types import TextBlock

from tracegraph.llm.base import Completion, LLMSettings


class AnthropicProvider:
    def __init__(self, settings: LLMSettings) -> None:
        if not settings.api_key:
            raise ValueError("LLM_PROVIDER=anthropic requires LLM_API_KEY")
        self.settings = settings
        self._client: Anthropic | None = None  # created on first call, never at import

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> Completion:
        if self._client is None:
            self._client = Anthropic(
                api_key=self.settings.api_key, timeout=self.settings.timeout_seconds
            )
        # Current Claude models reject sampling parameters (temperature), so none are sent;
        # JSON is requested through structured outputs (`output_config.format`).
        output_config: Any = (
            {"format": {"type": "json_schema", "schema": json_schema}}
            if json_schema is not None
            else omit
        )
        started = time.monotonic()
        response = self._client.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            messages=[{"role": "user", "content": prompt}],
            system=system or omit,
            output_config=output_config,
        )
        text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
        return Completion(
            text=text,
            latency_seconds=time.monotonic() - started,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
