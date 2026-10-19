"""Phase 1 Steps 1-2: parse, sample, synthesize, validate, and write staging Parquet files.

Run with `make stage`. Steps 1-2 need no database. Staging tables go to `data/processed/staging/`,
the only directory graph loaders read. Eval ground truth goes to `data/processed/` and is never
staged (ADR 003 §2).
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from tracegraph.config import Settings, settings
from tracegraph.ingest.ibm_aml import build_ibm_stage
from tracegraph.ingest.icij import build_icij_stage
from tracegraph.ingest.opensanctions import build_opensanctions_stage
from tracegraph.ingest.seed_links import build_customers
from tracegraph.ingest.staging_schemas import validate
from tracegraph.ingest.synthetic_profiles import attach_customers
from tracegraph.ingest.verify_data import read_manifest


def source_snapshot(raw: Path) -> str:
    """A compact identifier for the OpenSanctions and ICIJ snapshots used for seeding."""
    parts = []
    os_manifest = raw / "opensanctions" / "MANIFEST.tsv"
    if os_manifest.exists():
        rows = read_manifest(os_manifest)
        parts += [f"opensanctions:{row['dataset']}@{row['version']}" for row in rows]
    generated = sorted((raw / "icij" / "csv").glob("GENERATED_ON_*.txt"))
    if generated:
        parts.append(f"icij@{generated[-1].stem.removeprefix('GENERATED_ON_')}")
    return ";".join(parts) or "unknown"


def run(config: Settings) -> dict[str, object]:
    raw = config.raw_dir
    ibm = build_ibm_stage(raw / "ibm_aml", config)
    sanctions = build_opensanctions_stage(raw / "opensanctions", config)
    offshore = build_icij_stage(raw / "icij", config)

    accounts = attach_customers(ibm.accounts)
    snapshot = source_snapshot(raw)
    build = build_customers(
        accounts,
        ibm.laundering_account_ids,
        sanctions.entities,
        offshore.nodes,
        sanctions.address_vocabulary,
        config,
        snapshot,
    )
    customers, seed_truth = build.customers, build.ground_truth

    # Validate everything before writing anything, so a failure never leaves a partial stage.
    staging = {
        "ibm_transactions": ibm.transactions,
        "ibm_accounts": accounts,
        "ibm_banks": ibm.banks,
        "customers": customers,
        "opensanctions_entities": sanctions.entities,
        "opensanctions_relationships": sanctions.relationships,
        "icij_nodes": offshore.nodes,
        "icij_relationships": offshore.relationships,
    }
    staging = {name: validate(name, frame) for name, frame in staging.items()}

    config.staging_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in staging.items():
        frame.write_parquet(config.staging_dir / f"{name}.parquet")
    ibm.pattern_ground_truth.write_parquet(config.ibm_pattern_ground_truth_path)
    seed_truth.write_parquet(config.seed_ground_truth_path)
    ibm.seed_accounts.write_parquet(config.ibm_sample_seeds_path)
    build.name_edits.write_parquet(config.customer_name_edits_path)

    manifest: dict[str, object] = {
        "staged_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_snapshot": snapshot,
        "random_seed": config.random_seed,
        "row_counts": {name: frame.height for name, frame in staging.items()},
        "ground_truth_counts": {
            "ibm_pattern_ground_truth": ibm.pattern_ground_truth.height,
            "seed_ground_truth": seed_truth.height,
            "ibm_sample_seeds": ibm.seed_accounts.height,
        },
        "ibm": ibm.stats,
        "opensanctions": sanctions.stats,
        "icij": offshore.stats,
        "seeding": _seeding_summary(seed_truth, customers.height),
        "config": config.model_dump(
            mode="json",
            exclude={"neo4j_password", "anthropic_api_key", "openai_compat_api_key"},
        ),
    }
    (config.staging_dir / "stage_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _seeding_summary(truth: pl.DataFrame, customers: int) -> dict[str, object]:
    def counts(column: str, frame: pl.DataFrame) -> dict[str, int]:
        return {str(k): int(v) for k, v in frame.group_by(column).len().sort(column).iter_rows()}

    matches = truth.filter(pl.col("is_match"))
    return {
        "customers": customers,
        "seeded_matches": matches.height,
        "hard_negatives": truth.height - matches.height,
        "matches_by_source": counts("source_dataset", matches),
        "matches_by_reason": counts("seed_reason", matches),
        "perturbations": counts("perturbations", matches.explode("perturbations")),
    }


def main() -> int:
    manifest = run(settings)
    print(json.dumps({k: v for k, v in manifest.items() if k != "config"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
