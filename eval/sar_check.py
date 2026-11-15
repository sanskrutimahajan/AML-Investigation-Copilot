"""SAR draft check (`make sar-check`, ADR 010): draft a narrative for sampled alerts and report.

`sar_check_per_typology` alerts of each typology are sampled from the graph (seed `random_seed`)
and drafted through the full SAR path. Reported: how many drafts passed the grounding check (first
try, after one regeneration, or abstained), invalid-JSON failures, whether every draft carries the
DRAFT label, latency, and the LLM provider and model. Drafts that use accusatory words are listed
for review: a draft must state facts, never allege a crime.
Results go to eval/results/sar_check_<timestamp>.json and .md.
"""

import json
import random
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase, RoutingControl
from qdrant_client import QdrantClient

from tracegraph.config import Settings, settings
from tracegraph.er.embeddings import SentenceTransformerEmbedder
from tracegraph.generation.answer import QAContext
from tracegraph.generation.sar_drafter import DRAFT_LABEL, draft_sar
from tracegraph.llm.factory import get_provider, llm_settings

RESULTS_DIR = Path(__file__).parent / "results"
TYPOLOGIES = ("FAN-IN", "FAN-OUT", "CYCLE", "SCATTER-GATHER")
ACCUSATORY = re.compile(r"\b(launder\w*|fraud\w*|crim\w*|illegal\w*|illicit|guilt\w*)\b", re.I)


def sample_alerts(ctx: QAContext, config: Settings) -> list[str]:
    rng = random.Random(config.random_seed)
    picked = []
    for typology in TYPOLOGIES:
        records, _, _ = ctx.driver.execute_query(
            "MATCH (al:Alert {typology: $t}) RETURN al.alert_id AS id ORDER BY id",
            t=typology,
            database_=ctx.database,
            routing_=RoutingControl.READ,
        )
        ids = [r["id"] for r in records]
        picked += rng.sample(ids, min(config.sar_check_per_typology, len(ids)))
    return picked


def main(config: Settings = settings) -> int:
    ctx = QAContext(
        GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)),
        config.neo4j_database,
        QdrantClient(url=config.qdrant_url),
        SentenceTransformerEmbedder(config),
        get_provider(config),
        config,
    )
    rows: list[dict[str, Any]] = []
    for i, alert_id in enumerate(sample_alerts(ctx, config), start=1):
        draft = draft_sar(alert_id, ctx)
        drafts = draft.trace.get("drafts", [])
        alert = next((e for e in draft.evidence if e.type == "Alert"), None)
        rows.append({
            "alert_id": alert_id,
            "typology": alert and alert.props.get("typology"),
            "status": draft.status,
            "attempts": len(drafts),
            "json_failure": any("error" in d for d in drafts),
            "labeled": draft.text.startswith(DRAFT_LABEL),
            "accusatory_words": sorted({m.lower() for m in ACCUSATORY.findall(draft.text)}),
            "seconds": draft.trace.get("seconds"),
            "text": draft.text,
        })  # fmt: skip
        print(f"[{i}] {alert_id}: {draft.status}, attempts {len(drafts)}", flush=True)
    n = len(rows)
    drafted = [r for r in rows if r["status"] == "drafted"]
    seconds = sorted(r["seconds"] for r in rows if r["seconds"] is not None)
    summary = {
        "n_alerts": n,
        "drafted": len(drafted),
        "grounded_first_try": sum(r["attempts"] == 1 for r in drafted),
        "grounded_after_regeneration": sum(r["attempts"] == 2 for r in drafted),
        "abstained": sum(r["status"] == "insufficient_evidence" for r in rows),
        "json_failures": sum(r["json_failure"] for r in rows),
        "all_labeled": all(r["labeled"] for r in rows),
        "drafts_with_accusatory_words": [r["alert_id"] for r in rows if r["accusatory_words"]],
        "mean_seconds": round(sum(seconds) / len(seconds), 1) if seconds else None,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {"run_utc": stamp, "llm": llm_settings(config).describe(), "summary": summary,
              "drafts": rows}  # fmt: skip
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"sar_check_{stamp}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        f"# SAR check {stamp}",
        "",
        f"LLM: {report['llm']['provider']} / {report['llm']['model']}. Alerts: {n} "
        f"({config.sar_check_per_typology} per typology, seed {config.random_seed}).",
        "",
        f"- Drafted (grounded): {summary['drafted']}/{n}; first try "
        f"{summary['grounded_first_try']}, after one regeneration "
        f"{summary['grounded_after_regeneration']}",
        f"- Abstained (insufficient evidence): {summary['abstained']}; invalid JSON: "
        f"{summary['json_failures']}",
        f"- Every draft labeled '{DRAFT_LABEL}': {summary['all_labeled']}",
        f"- Drafts with accusatory words (for review): {summary['drafts_with_accusatory_words']}",
        f"- Mean latency: {summary['mean_seconds']} s",
    ]
    path.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
