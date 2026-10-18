"""Provider for OpenAI-compatible chat-completion endpoints."""

from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

from tracegraph.config import Settings, settings


class OpenAICompatProvider:
    """Generate text through an OpenAI-compatible endpoint such as vLLM."""

    def __init__(self, config: Settings = settings) -> None:
        self._model = config.openai_compat_model
        self._enabled = config.llm_enabled
        self._api_key = config.openai_compat_api_key
        self._base_url = config.openai_compat_base_url
        self._client: AsyncOpenAI | None = None

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Generate text only when explicitly enabled with a configured API key."""
        if not self._enabled:
            raise RuntimeError(
                "LLM calls are disabled; explicit user approval is required to enable."
            )
        if not self._api_key:
            raise RuntimeError("OPENAI_COMPAT_API_KEY is required when LLM calls are enabled.")
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
            )

        messages: list[
            ChatCompletionSystemMessageParam | ChatCompletionUserMessageParam
        ] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("OpenAI-compatible provider returned an empty response")
        return content
