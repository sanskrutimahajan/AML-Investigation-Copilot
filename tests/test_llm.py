"""LLM provider selection from config and structured JSON output (mocked; no network)."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from tracegraph.config import Settings
from tracegraph.llm.anthropic_provider import AnthropicProvider
from tracegraph.llm.base import Completion, LLMSettings
from tracegraph.llm.factory import get_provider, llm_settings
from tracegraph.llm.openai_compat_provider import OpenAICompatProvider
from tracegraph.llm.structured import StructuredOutputError, generate_json


def _config(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def test_default_is_local_ollama_and_needs_no_key() -> None:
    provider = get_provider(_config())
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.settings.provider == "ollama"
    assert provider.settings.base_url == "http://localhost:11434/v1"
    assert provider.settings.model == "qwen2.5:7b-instruct"
    assert provider._client is None  # nothing is created until the first call


@pytest.mark.parametrize(
    ("values", "cls", "model"),
    [
        (
            {"llm_provider": "ollama", "llm_model": "llama3.1:8b"},
            OpenAICompatProvider,
            "llama3.1:8b",
        ),
        (
            {
                "llm_provider": "openai_compatible",
                "llm_base_url": "https://api.groq.com/openai/v1",
                "llm_api_key": "test-key",
                "llm_model": "llama-3.1-8b-instant",
            },
            OpenAICompatProvider,
            "llama-3.1-8b-instant",
        ),
        (
            {
                "llm_provider": "anthropic",
                "llm_api_key": "test-key",
                "llm_model": "claude-sonnet-5-5",
            },
            AnthropicProvider,
            "claude-sonnet-5-5",
        ),
    ],
)
def test_each_provider_is_selected_from_config(
    values: dict[str, Any], cls: type, model: str
) -> None:
    provider = get_provider(_config(**values))
    assert isinstance(provider, cls)
    assert provider.settings.model == model


def test_anthropic_requires_a_key() -> None:
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        get_provider(_config(llm_provider="anthropic"))


def test_results_files_never_record_the_key() -> None:
    described = llm_settings(_config(llm_provider="anthropic", llm_api_key="secret")).describe()
    assert described["model"] and "secret" not in str(described) and "api_key" not in described


def test_openai_compatible_call_sends_schema_and_parses_reply() -> None:
    reply = MagicMock()
    reply.choices[0].message.content = '{"ok": true}'
    reply.model, reply.usage = "qwen2.5:7b-instruct", None
    client = MagicMock()
    client.chat.completions.create.return_value = reply
    provider = get_provider(_config())
    with patch("tracegraph.llm.openai_compat_provider.OpenAI", return_value=client) as cls:
        out = provider.complete("hi", system="s", json_schema={"type": "object"})
    assert out.text == '{"ok": true}'
    assert cls.call_args.kwargs["api_key"] == "ollama"
    sent = client.chat.completions.create.call_args.kwargs
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["messages"][0] == {"role": "system", "content": "s"}


class Verdict(BaseModel):
    decision: str
    confidence: float


class ScriptedProvider:
    def __init__(self, replies: list[str]) -> None:
        self.settings = LLMSettings("ollama", "http://x", "", "m", 10, 0.0, 1.0)
        self.replies, self.prompts = replies, []

    def complete(self, prompt: str, **_: Any) -> Completion:
        self.prompts.append(prompt)
        return Completion(self.replies.pop(0), 0.5, "m")


def test_generate_json_validates_and_retries_once() -> None:
    provider = ScriptedProvider(
        ['{"decision": "match"}', '```json\n{"decision": "match", "confidence": 0.9}\n```']
    )
    result = generate_json(provider, "judge this", Verdict, retries=1)
    assert result.value == Verdict(decision="match", confidence=0.9)
    assert result.attempts == 2 and result.latency_seconds == 1.0
    assert "not valid" in provider.prompts[1]  # the retry explains the error


def test_strict_schema_closes_objects() -> None:
    from tracegraph.llm.structured import strict_schema

    schema = strict_schema(Verdict.model_json_schema())
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["confidence", "decision"]


def test_anthropic_sends_no_temperature_and_uses_output_config() -> None:
    reply = MagicMock()
    reply.content, reply.model = [], "claude-sonnet-5-5"
    reply.usage.input_tokens, reply.usage.output_tokens = 1, 1
    client = MagicMock()
    client.messages.create.return_value = reply
    provider = get_provider(_config(llm_provider="anthropic", llm_api_key="k"))
    with patch("tracegraph.llm.anthropic_provider.Anthropic", return_value=client):
        provider.complete("hi", json_schema={"type": "object"})
    sent = client.messages.create.call_args.kwargs
    assert "temperature" not in sent
    assert sent["output_config"]["format"]["type"] == "json_schema"


def test_generate_json_gives_up_after_the_retry_budget() -> None:
    provider = ScriptedProvider(["not json", "still not json"])
    with pytest.raises(StructuredOutputError, match="2 attempts"):
        generate_json(provider, "judge this", Verdict, retries=1)
