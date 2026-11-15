"""Provider-agnostic LLM interface (ADR 008).

Every LLM step (router, adjudicator, text-to-Cypher, answers, SAR drafts) calls `complete()` on a
provider built from config by `tracegraph.llm.factory`, so switching providers is a .env change.
"""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

ProviderName = Literal["ollama", "openai_compatible", "anthropic"]


@dataclass(frozen=True)
class LLMSettings:
    """Resolved LLM settings."""

    provider: ProviderName
    base_url: str
    api_key: str
    model: str
    max_tokens: int
    temperature: float
    timeout_seconds: float

    def describe(self) -> dict[str, str]:
        """What eval results record about the model (never the key)."""
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url}


@dataclass(frozen=True)
class Completion:
    text: str
    latency_seconds: float
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProvider(Protocol):
    settings: LLMSettings

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> Completion:
        """One completion. With `json_schema`, the provider is asked for matching JSON."""
        ...
