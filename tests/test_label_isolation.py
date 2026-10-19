"""Labels and ground truth must never reach the graph (ADR 003 §2)."""

from pathlib import Path

import polars as pl
import pytest
from raw_fixtures import fixture_settings, write_all

import tracegraph.graph
from tracegraph.ingest import stage
from tracegraph.ingest.staging_schemas import LABEL_COLUMNS, STAGING_SCHEMAS, validate


def test_staging_schemas_do_not_declare_label_columns() -> None:
    for name, schema in STAGING_SCHEMAS.items():
        assert not LABEL_COLUMNS & set(schema.columns), name
        assert schema.strict is True, name


def test_validate_rejects_a_transaction_label_column() -> None:
    frame = pl.DataFrame({"transaction_id": ["ibm-1"], "is_laundering": [True]})
    with pytest.raises(ValueError, match="label columns"):
        validate("ibm_transactions", frame)


def test_graph_modules_never_reference_ground_truth() -> None:
    graph_dir = Path(tracegraph.graph.__file__).parent
    for module in graph_dir.glob("*.py"):
        source = module.read_text()
        assert "ground_truth" not in source, module.name
        assert "is_laundering" not in source, module.name


def test_staging_run_keeps_ground_truth_outside_staging(tmp_path: Path) -> None:
    write_all(tmp_path)
    config = fixture_settings(tmp_path)
    manifest = stage.run(config)

    staged = sorted(p.name for p in config.staging_dir.glob("*.parquet"))
    assert staged == sorted(f"{name}.parquet" for name in STAGING_SCHEMAS)
    for path in config.staging_dir.glob("*.parquet"):
        assert not LABEL_COLUMNS & set(pl.read_parquet_schema(path)), path.name
    assert config.ibm_pattern_ground_truth_path.exists()
    assert config.seed_ground_truth_path.exists()
    assert config.ibm_sample_seeds_path.exists()
    assert config.staging_dir not in config.ibm_sample_seeds_path.parents
    assert config.staging_dir not in config.seed_ground_truth_path.parents
    assert config.staging_dir not in config.ibm_pattern_ground_truth_path.parents

    counts = manifest["row_counts"]
    assert isinstance(counts, dict)
    assert (
        counts["ibm_transactions"]
        == pl.read_parquet(config.staging_dir / "ibm_transactions.parquet").height
    )
    assert "neo4j_password" not in str(manifest["config"])
