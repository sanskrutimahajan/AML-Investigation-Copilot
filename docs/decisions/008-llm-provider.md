# ADR 008: LLM provider — local Ollama by default, any provider through config

- **Status:** Accepted (2026-11-14)

## Context
The approved Phase 2 plan used the Anthropic API for Step B, with a cost estimate and a hard spend
cap. The user then directed (2026-10-20) that no paid API be used by default, and that the project
work with any provider through `.env` alone, with no code changes.

## Decisions
1. **Default:** Ollama, running natively on macOS, with `qwen2.5:7b-instruct` (fits an M2 with 24 GB).
2. **Configuration:** `.env` only.
   - `LLM_PROVIDER=ollama|openai_compatible|anthropic`, plus `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`.
   - Setting a remote provider and key in your own `.env` is the opt-in to a paid API. None is committed.
3. **One way to get a provider:** `tracegraph.llm.factory.get_provider(config)`.
   - Ollama and every OpenAI-compatible endpoint share `OpenAICompatProvider`. It falls back to
     `json_object` mode if a server rejects `json_schema`.
   - `AnthropicProvider` sends no `temperature` (current Claude models reject sampling parameters)
     and requests JSON through `output_config.format`.
4. **Structured output:** `llm.structured.generate_json`.
   - Strict JSON schema from the Pydantic model, then validation, with one retry
     (`llm_json_retries`) that tells the model what was wrong.
   - Every attempt is recorded, so callers report retry and failure counts.
5. **No spend cap:** nothing is billed by default, so the planned cap has nothing to limit. Run
   time is what Step B costs, and the review-band cap (`er_review_queue_cap`) bounds it.
6. **Secrets stay out of outputs:** manifests exclude every `*password*` and `*api_key*` setting.
   The rename to `llm_api_key` would otherwise have written the key into `stage_manifest.json`.

## Evidence
- **Tests:** 11 mocked provider tests at the time, with no network: provider selection for each
  of the three providers, a separate judge model's settings (since removed, see the update below),
  the schema being sent, no temperature for Anthropic, one JSON retry, and giving up after the
  retry budget.
- **Live smoke test (2026-11-14):** Ollama with `qwen2.5:7b-instruct`, on 20 dev review-band pairs
  through the Step B adjudicator, run twice (two prompt versions).
  - 40 of 40 replies were valid JSON on the first attempt, with no retries.
  - Mean latency was 2.1 s per pair for the first prompt and 6.8 s for the second, which asks for a
    longer reason. The maximum was 10.7 s.
  - The smoke test measured only whether the provider works. Decision quality is a Step B result
    and is reported by `make er-tune` / `make er-test` (ADR 007).

## Update (2026-11-15)
RAGAS is dropped (user decision): `eval/qa_check.py`, with gold answers, measures correctness
directly, and the grounding check covers what a faithfulness judge would. The `JUDGE_*` settings
and the judge role of `get_provider` existed only for RAGAS, so they are removed. Results files
still record the provider and model.
