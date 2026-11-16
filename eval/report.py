"""Evaluation report (`make eval`): one summary of every result the README reports.

The held-out evaluations (leak test, QA check, detection test, SAR check) are each run once by
their own commands, and re-running them here would break that discipline. This script collects
the result files the README cites, fails if any is missing, and writes one timestamped JSON and
Markdown report with the headline numbers and the LLM provider and model behind each result.
Results go to eval/results/eval_<timestamp>.json and .md.
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "eval" / "results"
CITED = re.compile(r"eval/results/([A-Za-z0-9_.-]+\.json)")


def cited_files(readme: str) -> list[str]:
    """Result files linked from the README, in order of first mention."""
    return list(dict.fromkeys(CITED.findall(readme)))


def _rate(metric: dict[str, Any]) -> dict[str, Any]:
    return {"rate": metric["rate"], "n": metric["n"], "ci95": metric["ci95"]}


def summarize(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Headline numbers of one result file, by its kind (from the file name)."""
    base: dict[str, Any] = {"file": name, "run_utc": result.get("run_utc")}
    if "llm" in result:
        base["llm"] = {k: result["llm"].get(k) for k in ("provider", "model")}
    if name.startswith("qa_check"):
        s = result["summary"]
        return base | {
            "kind": "qa_check",
            "question_seed": result.get("question_seed"),
            "n_questions": s["n_questions"],
            "answer_accuracy": _rate(s["answer_accuracy"]),
            "intent_accuracy": _rate(s["intent_accuracy"]),
            "link_accuracy": _rate(s["link_accuracy"]),
            "answer_accuracy_by_family": {
                family: _rate(m) for family, m in s["answer_accuracy_by_family"].items()
            },
        }
    if name.startswith("detection"):
        return base | {
            "kind": f"detection_{result['split']}",
            "seed_accounts": result["population"]["seed_accounts"],
            "base_rate_core": result["population"]["base_rate_core"],
            "detectors": {
                typology: {
                    "size": d["size"],
                    "window_hours": d["window_hours"],
                    "pattern_recall": d["detector"]["pattern_recall"],
                    "patterns": f"{d['detector']['patterns_caught']}/{d['detector']['patterns']}",
                    "precision": d["detector"]["precision"],
                    "baseline_pattern_recall": d["degree_baseline"]["pattern_recall"],
                    "baseline_precision": d["degree_baseline"]["precision"],
                    **d["detector_minus_baseline"],
                }
                for typology, d in result["detectors"].items()
            },
        }
    if name.startswith("sar_check"):
        return base | {"kind": "sar_check", **result["summary"]}
    if name.startswith("leak_test"):
        return base | {
            "kind": "leak_test",
            "max_auc": result["max_auc"],
            "target_auc": result["target_auc"],
            "verdict": result.get("verdict"),
        }
    return base | {"kind": "other"}


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Evaluation report {report['run_utc']}",
        "",
        "Collected from the result files the README cites; nothing was re-run.",
        "",
    ]
    for s in report["results"]:
        llm = f" — LLM: {s['llm']['provider']} / {s['llm']['model']}" if "llm" in s else ""
        lines += [f"## {s['kind']} (`{s['file']}`){llm}", ""]
        if s["kind"] == "qa_check":
            lines += [
                f"Question seed {s['question_seed']}, {s['n_questions']} questions.",
                "",
                "| Measure | Rate | n | 95% CI |",
                "| --- | --- | --- | --- |",
            ]
            measures = {k: s[k] for k in ("answer_accuracy", "intent_accuracy", "link_accuracy")}
            measures |= {f"answer: {f}": m for f, m in s["answer_accuracy_by_family"].items()}
            for label, m in measures.items():
                lo, hi = (max(x, 0.0) + 0.0 for x in m["ci95"])  # + 0.0 turns -0.0 into 0.0
                lines.append(f"| {label} | {m['rate']:.3f} | {m['n']} | {lo:.2f}–{hi:.2f} |")
        elif s["kind"].startswith("detection"):
            lines += [
                f"{s['seed_accounts']} seed accounts, base rate {s['base_rate_core']:.1%}.",
                "",
                "| Detector | Recall | Precision | Baseline recall | Baseline precision |",
                "| --- | --- | --- | --- | --- |",
            ]
            for typology, d in s["detectors"].items():
                lines.append(
                    f"| {typology} | {d['patterns']} ({d['pattern_recall']:.2f}) | "
                    f"{d['precision']:.2f} | {d['baseline_pattern_recall']:.2f} | "
                    f"{d['baseline_precision']:.2f} |"
                )
        elif s["kind"] == "sar_check":
            lines.append(
                f"{s['drafted']}/{s['n_alerts']} grounded ({s['grounded_first_try']} first try), "
                f"{s['abstained']} abstained, {s['json_failures']} invalid JSON, "
                f"all labeled: {s['all_labeled']}, mean {s['mean_seconds']} s."
            )
        elif s["kind"] == "leak_test":
            lines.append(
                f"Max ROC-AUC {s['max_auc']} (target ≤ {s['target_auc']}); file verdict "
                f"{s['verdict']}. The accepted ceiling and its reason are in ADR 006 and the "
                "README."
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    readme = (ROOT / "README.md").read_text()
    names = cited_files(readme)
    missing = [n for n in names if not (RESULTS_DIR / n).exists()]
    if missing:
        print(f"README cites result files that do not exist: {missing}", file=sys.stderr)
        return 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_utc": stamp,
        "source": "README-cited result files; nothing re-run",
        "results": [summarize(n, json.loads((RESULTS_DIR / n).read_text())) for n in names],
    }
    path = RESULTS_DIR / f"eval_{stamp}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    path.with_suffix(".md").write_text(markdown(report))
    print(markdown(report))
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
