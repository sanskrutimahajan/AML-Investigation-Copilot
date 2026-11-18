"""MeteredProvider records tokens and latency per call and reports unknown usage honestly."""

from typing import Any

from tracegraph.llm.base import Completion
from tracegraph.llm.metering import MeteredProvider


class _Fake:
    settings: Any = None

    def __init__(self, usage: list[tuple[int | None, int | None]]) -> None:
        self._usage = iter(usage)

    def complete(self, prompt: str, **_: Any) -> Completion:
        tokens_in, tokens_out = next(self._usage)
        return Completion("{}", 0.5, "m", tokens_in, tokens_out)


def test_usage_totals_and_marks() -> None:
    meter = MeteredProvider(_Fake([(10, 2), (20, 3), (5, 1)]))
    meter.complete("a")
    start = meter.mark()
    meter.complete("b")
    meter.complete("c")
    assert meter.usage() == {"llm_calls": 3, "input_tokens": 35, "output_tokens": 6,
                             "llm_seconds": 1.5}  # fmt: skip
    assert meter.usage(since=start)["input_tokens"] == 25


def test_missing_usage_is_unknown_not_zero() -> None:
    meter = MeteredProvider(_Fake([(10, 2), (None, None)]))
    meter.complete("a")
    meter.complete("b")
    usage = meter.usage()
    assert usage["input_tokens"] is None and usage["output_tokens"] is None
    assert usage["llm_calls"] == 2
