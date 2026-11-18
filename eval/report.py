"""Evaluation report (`make eval`): one summary of every result the README and RESULTS.md report.

The held-out evaluations (leak test, QA check, detection test, SAR check) are each run once by
their own commands, and re-running them here would break that discipline. This script collects
the result files cited in README.md and docs/RESULTS.md, fails if any is missing, and writes one
timestamped JSON and Markdown report with the headline numbers and the LLM provider and model
behind each result.
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
# Documents whose result links `make eval` collects; a missing document is skipped.
CITING_DOCS = ("README.md", "docs/RESULTS.md")


def cited_files(readme: str) -> list[str]:
    """Result files linked from the given text, in order of first mention."""
    return list(dict.fromkeys(CITED.findall(readme)))


def _rate(metric: dict[str, Any]) -> dict[str, Any]:
    return {"rate": metric["rate"], "n": metric["n"], "ci95": metric["ci95"]}


def _detector_summary(d: dict[str, Any]) -> dict[str, Any]:
    """One detector's headline numbers. Detectors without pattern labels (rapid movement) have
    no recall; structuring is scored on injected cases only (ADR 015)."""
    if "injected_cases" in d:
        return {"injected": f"{d['injected_caught']}/{d['injected_cases']}",
                "decoy_false_positives": d["decoy_false_positives"],
                "real_accounts_flagged": d["real_data"]["accounts_flagged"]}  # fmt: skip
    det, base = d["detector"], d["degree_baseline"]
    labeled = "pattern_recall" in det
    return {
        "size": d["size"],
        "window_hours": d["window_hours"],
        "pattern_recall": det["pattern_recall"] if labeled else None,
        "patterns": f"{det['patterns_caught']}/{det['patterns']}" if labeled else None,
        "precision": det["precision"],
        "baseline_pattern_recall": base["pattern_recall"] if labeled else None,
        "baseline_precision": base["precision"],
        **d["detector_minus_baseline"],
    }


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
                typology: _detector_summary(d) for typology, d in result["detectors"].items()
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
    if name.startswith("er_"):
        variant = result["decision_variant"]
        model = result["models"][variant]
        step_b = result.get("llm_adjudication", {})
        final = step_b.get("with_llm") if "with_llm" in step_b else None
        llm_written = bool(result.get("llm_decides_edges", True)) and final is not None
        errors = step_b if llm_written else result["breakdowns_decision_model"]
        return base | {
            "kind": f"er_{result['split']}",
            "decision_variant": variant,
            "llm_decides_edges": llm_written,
            "match_threshold": result["thresholds"][variant],
            "review_threshold": result["thresholds"]["review"],
            "n_positive_pairs": result["n_positive_pairs"],
            "splink": {k: model[k] for k in ("precision", "recall", "f1", "f1_ci95")},
            "with_llm": (
                {k: final[k] for k in ("precision", "recall", "f1", "f1_ci95")}
                | {"f1_gain_ci95": step_b["f1_gain_ci95"]}
                if final
                else None
            ),
            "baseline_f1": {
                name: result["models"][name]["f1"]
                for name in result["models"]
                if name.startswith("baseline_")
            },
            "homonym_fp_rate": errors["homonyms"]["false_positive_rate_same_as"],
            "lookalike_fp_rate": errors["lookalike_false_positive_rate"]["any_record_matched"],
        }
    return base | {"kind": "other"}


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Evaluation report {report['run_utc']}",
        "",
        "Collected from the result files the README and docs/RESULTS.md cite; nothing was re-run.",
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
                if "injected" in d:
                    lines.append(f"| {typology} | injected {d['injected']} (logic check only) | "
                                 f"— | — | — |")  # fmt: skip
                elif d["pattern_recall"] is None:
                    lines.append(f"| {typology} | not measured | {d['precision']:.2f} | — | "
                                 f"{d['baseline_precision']:.2f} |")  # fmt: skip
                else:
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
        elif s["kind"].startswith("er_"):
            lines += [
                f"Decision model `{s['decision_variant']}`, match ≥ {s['match_threshold']}, "
                f"review band ≥ {s['review_threshold']}; {s['n_positive_pairs']} true pairs.",
                "",
                "| Model | Precision | Recall | F1 | F1 95% CI |",
                "| --- | --- | --- | --- | --- |",
            ]
            llm_label = "Splink + LLM" if s["llm_decides_edges"] else "Splink + LLM (not used)"
            rows = {"Splink": s["splink"]} | ({llm_label: s["with_llm"]} if s["with_llm"] else {})
            for label, m in rows.items():
                lo, hi = m["f1_ci95"]
                lines.append(
                    f"| {label} | {m['precision']:.3f} | {m['recall']:.3f} | {m['f1']:.3f} | "
                    f"{lo:.3f}–{hi:.3f} |"
                )
            baselines = ", ".join(f"{k} {v:.3f}" for k, v in s["baseline_f1"].items())
            lines += [
                "",
                "Written edges: "
                + ("Splink + LLM" if s["llm_decides_edges"] else "Splink alone")
                + f". Baseline F1: {baselines}. Homonym false-positive rate of written edges "
                f"{s['homonym_fp_rate']:.3f}, look-alike {s['lookalike_fp_rate']:.3f}.",
            ]
        elif s["kind"] == "leak_test":
            lines.append(
                f"Max ROC-AUC {s['max_auc']} (target ≤ {s['target_auc']}); file verdict "
                f"{s['verdict']}. The accepted ceiling and its reason are in ADR 006 and the "
                "docs/RESULTS.md."
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    docs = [ROOT / d for d in CITING_DOCS if (ROOT / d).exists()]
    names = cited_files("\n".join(d.read_text() for d in docs))
    missing = [n for n in names if not (RESULTS_DIR / n).exists()]
    if missing:
        print(f"Cited result files that do not exist: {missing}", file=sys.stderr)
        return 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_utc": stamp,
        "source": "result files cited in README.md and docs/RESULTS.md; nothing re-run",
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
