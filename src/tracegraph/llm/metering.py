"""Token and latency metering around any LLM provider.

`MeteredProvider` wraps a provider and records every call (input and output tokens, latency), so
the API can return and log per-request usage and eval runs can report totals. Providers that do
not return usage leave the token counts as None; they are reported as unknown, never as zero.
"""

import threading
from dataclasses import dataclass, field
from typing import Any

from tracegraph.llm.base import Completion, LLMProvider


@dataclass
class _Call:
    input_tokens: int | None
    output_tokens: int | None
    latency_seconds: float


@dataclass
class MeteredProvider:
    inner: LLMProvider
    calls: list[_Call] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def settings(self) -> Any:
        return self.inner.settings

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> Completion:
        completion = self.inner.complete(prompt, system=system, json_schema=json_schema)
        with self._lock:
            self.calls.append(
                _Call(completion.input_tokens, completion.output_tokens, completion.latency_seconds)
            )
        return completion

    def mark(self) -> int:
        """A position to measure from (see `usage(since=...)`)."""
        with self._lock:
            return len(self.calls)

    def usage(self, since: int = 0) -> dict[str, Any]:
        """Calls, token totals (None if any call lacked usage) and LLM time since `since`."""
        with self._lock:
            calls = self.calls[since:]
        return {
            "llm_calls": len(calls),
            "input_tokens": _total([c.input_tokens for c in calls]),
            "output_tokens": _total([c.output_tokens for c in calls]),
            "llm_seconds": round(sum(c.latency_seconds for c in calls), 3),
        }


def _total(values: list[int | None]) -> int | None:
    return None if any(v is None for v in values) else sum(v or 0 for v in values)
