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


def test_main_collects_files_cited_in_docs_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "eval" / "results"
    results.mkdir(parents=True)
    (results / "qa_check_1.json").write_text(json.dumps(QA))
    (tmp_path / "README.md").write_text("no result links here")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "RESULTS.md").write_text("[r](../eval/results/missing.json)")
    monkeypatch.setattr(report, "ROOT", tmp_path)
    monkeypatch.setattr(report, "RESULTS_DIR", results)
    assert report.main() == 1

    (tmp_path / "docs" / "RESULTS.md").write_text("[r](../eval/results/qa_check_1.json)")
    assert report.main() == 0
    written = json.loads(sorted(results.glob("eval_*.json"))[0].read_text())
    assert [r["file"] for r in written["results"]] == ["qa_check_1.json"]


ER = {
    "run_utc": "20261118T000000Z",
    "split": "test",
    "decision_variant": "splink_no_embeddings",
    "thresholds": {"splink_no_embeddings": 0.99, "review": 0.9},
    "n_positive_pairs": 10,
    "llm": {"provider": "ollama", "model": "m", "prompt_version": "v2"},
    "models": {
        "splink_no_embeddings": {
            "precision": 0.5,
            "recall": 0.6,
            "f1": 0.55,
            "f1_ci95": [0.4, 0.7],
        },
        "baseline_exact": {"precision": 0.6, "recall": 0.2, "f1": 0.3},
    },
    "breakdowns_decision_model": {
        "homonyms": {"false_positive_rate_same_as": 0.8},
        "lookalike_false_positive_rate": {"any_record_matched": 0.01},
    },
    "llm_adjudication": {
        "with_llm": {"precision": 0.55, "recall": 0.65, "f1": 0.6, "f1_ci95": [0.45, 0.75]},
        "f1_gain_ci95": [0.0, 0.1],
        "homonyms": {"false_positive_rate_same_as": 0.7},
        "lookalike_false_positive_rate": {"any_record_matched": 0.0},
    },
}


def test_er_summary_uses_the_decision_model_and_final_llm_edges() -> None:
    summary = report.summarize("er_test_final.json", ER)
    assert summary["kind"] == "er_test" and summary["llm"] == {"provider": "ollama", "model": "m"}
    assert summary["splink"]["f1"] == 0.55 and summary["with_llm"]["f1"] == 0.6
    assert summary["homonym_fp_rate"] == 0.7  # after the LLM, not before
    md = report.markdown({"run_utc": "t", "results": [summary]})
    assert "Splink + LLM" in md and "baseline_exact 0.300" in md


def test_er_summary_without_step_b_reports_splink_only() -> None:
    result = ER | {"llm_adjudication": {"skipped": "Step B has not run"}}
    result.pop("llm")
    summary = report.summarize("er_dev_1.json", result)
    assert summary["with_llm"] is None and summary["homonym_fp_rate"] == 0.8
    assert "Splink + LLM" not in report.markdown({"run_utc": "t", "results": [summary]})


def test_er_summary_reports_splink_errors_when_the_llm_does_not_decide() -> None:
    summary = report.summarize("er_test_final.json", ER | {"llm_decides_edges": False})
    assert summary["llm_decides_edges"] is False and summary["homonym_fp_rate"] == 0.8
    md = report.markdown({"run_utc": "t", "results": [summary]})
    assert "Splink + LLM (not used)" in md and "Written edges: Splink alone" in md


def test_detectors_without_labels_or_with_injected_cases_are_summarized() -> None:
    rapid = {"size": 1, "window_hours": 168.0,
             "detector": {"flagged_seed_accounts": 10, "precision": 0.6},
             "degree_baseline": {"precision": 0.86, "pattern_recall": 0.0},
             "detector_minus_baseline": {"precision_diff_ci95": [-0.3, -0.2]}}  # fmt: skip
    structuring = {"injected_cases": 40, "injected_caught": 40,
                   "decoy_false_positives": {"not_cash": "0/40"},
                   "real_data": {"accounts_flagged": 7}}  # fmt: skip
    assert report._detector_summary(rapid)["pattern_recall"] is None
    assert report._detector_summary(structuring) == {
        "injected": "40/40", "decoy_false_positives": {"not_cash": "0/40"},
        "real_accounts_flagged": 7}  # fmt: skip
