"""OpenAI-compatible chat completions: Ollama (default), OpenAI, Groq, Together, vLLM, LM Studio."""

import time
from typing import Any

from openai import BadRequestError, OpenAI

from tracegraph.llm.base import Completion, LLMSettings


class OpenAICompatProvider:
    """Calls `/chat/completions` on `settings.base_url`. Ollama needs no key."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self._client: OpenAI | None = None  # created on first call, never at import

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(
                base_url=self.settings.base_url,
                # Ollama ignores the key; the client library requires a non-empty value.
                api_key=self.settings.api_key or "ollama",
                timeout=self.settings.timeout_seconds,
            )
        return self._client

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> Completion:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
        }
        if json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema, "strict": True},
            }
        started = time.monotonic()
        try:
            response = self._get_client().chat.completions.create(**kwargs)
        except BadRequestError:
            if json_schema is None:
                raise
            # Some compatible servers only support plain JSON mode; the schema is then enforced
            # by validation in `structured.generate_json`.
            kwargs["response_format"] = {"type": "json_object"}
            response = self._get_client().chat.completions.create(**kwargs)
        latency = time.monotonic() - started
        usage = response.usage
        return Completion(
            text=response.choices[0].message.content or "",
            latency_seconds=latency,
            model=response.model or self.settings.model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )
