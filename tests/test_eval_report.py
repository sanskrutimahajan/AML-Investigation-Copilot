"""`make eval` collects README-cited result files; it never re-runs an evaluation."""

import json
from pathlib import Path

import pytest

from eval import report

QA = {
    "run_utc": "20261115T000000Z",
    "question_seed": 44,
    "llm": {"provider": "ollama", "model": "m", "base_url": "http://x"},
    "summary": {
        "n_questions": 2,
        "answer_accuracy": {"rate": 0.5, "n": 2, "ci95": [-0.0, 0.9]},
        "intent_accuracy": {"rate": 1.0, "n": 2, "ci95": [0.3, 1.0]},
        "link_accuracy": {"rate": 1.0, "n": 2, "ci95": [0.3, 1.0]},
        "answer_accuracy_by_family": {"paths": {"rate": 0.5, "n": 2, "ci95": [0.1, 0.9]}},
    },
    "questions": [],
}


def test_cited_files_are_unique_and_in_order() -> None:
    text = "[a](eval/results/qa_check_1.json) and eval/results/leak_test_2.json, again " \
        "(eval/results/qa_check_1.json)"  # fmt: skip
    assert report.cited_files(text) == ["qa_check_1.json", "leak_test_2.json"]


def test_summary_keeps_the_llm_and_markdown_shows_no_negative_zero() -> None:
    summary = report.summarize("qa_check_1.json", QA)
    assert summary["kind"] == "qa_check" and summary["llm"] == {"provider": "ollama", "model": "m"}
    md = report.markdown({"run_utc": "t", "results": [summary]})
    assert "ollama / m" in md and "0.00–0.90" in md and "-0.00" not in md


def test_main_fails_when_a_cited_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    (results / "qa_check_1.json").write_text(json.dumps(QA))
    readme = tmp_path / "README.md"
    readme.write_text("[r](eval/results/qa_check_1.json) [m](eval/results/missing.json)")
    monkeypatch.setattr(report, "ROOT", tmp_path)
    monkeypatch.setattr(report, "RESULTS_DIR", results)
    assert report.main() == 1

    readme.write_text("[r](eval/results/qa_check_1.json)")
    assert report.main() == 0
    written = sorted(results.glob("eval_*.json"))
    assert len(written) == 1 and written[0].with_suffix(".md").exists()
