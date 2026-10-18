"""Shared asynchronous interface for text-generation providers."""

from typing import Protocol


class LLMProvider(Protocol):
    """Provider contract consumed by generation and adjudication code."""

    async def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Generate one text response for a prompt."""
        ...
