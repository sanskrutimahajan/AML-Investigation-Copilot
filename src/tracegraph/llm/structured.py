"""Structured JSON with schema validation and bounded retries (router, adjudicator, grounding).

The provider is asked for JSON matching the Pydantic model's schema. The reply is validated, and on
a parse or validation failure the model is told what was wrong and asked again, at most
`llm_json_retries` times. Every attempt is recorded, so callers can report failure rates.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from tracegraph.llm.base import Completion, LLMProvider

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(RuntimeError):
    """The model did not return valid JSON for the schema within the retry budget."""


@dataclass
class StructuredResult(Generic[T]):
    value: T
    attempts: int
    completions: list[Completion] = field(default_factory=list)

    @property
    def latency_seconds(self) -> float:
        return sum(c.latency_seconds for c in self.completions)


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic JSON schema made strict: every object closed and every property required.

    Required by OpenAI strict mode and Anthropic structured outputs; harmless for Ollama.
    """

    def visit(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: visit(v) for k, v in node.items()}
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            return node
        if isinstance(node, list):
            return [visit(v) for v in node]
        return node

    result: dict[str, Any] = visit(schema)
    return result


def _extract_json(text: str) -> str:
    """Strip code fences some models wrap around JSON."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()


def generate_json(
    provider: LLMProvider,
    prompt: str,
    schema: type[T],
    *,
    system: str | None = None,
    retries: int = 1,
) -> StructuredResult[T]:
    json_schema = strict_schema(schema.model_json_schema())
    completions: list[Completion] = []
    message = prompt
    last_error = ""
    for attempt in range(1, retries + 2):
        completion = provider.complete(message, system=system, json_schema=json_schema)
        completions.append(completion)
        try:
            value = schema.model_validate(json.loads(_extract_json(completion.text)))
            return StructuredResult(value=value, attempts=attempt, completions=completions)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:500]
            message = (
                f"{prompt}\n\nYour previous reply was not valid for the required JSON schema:\n"
                f"{last_error}\nReply again with only a JSON object that matches the schema."
            )
    raise StructuredOutputError(f"invalid JSON after {retries + 1} attempts: {last_error}")
