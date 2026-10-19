"""IBM AML parsing and pattern-preserving sampling."""

from datetime import datetime
from pathlib import Path

import polars as pl
import pytest
from raw_fixtures import IBM_HEADER, fixture_settings, write_ibm

from tracegraph.ingest import ibm_aml


@pytest.fixture
def ibm_dir(tmp_path: Path) -> Path:
    return write_ibm(tmp_path / "raw")


def test_transactions_normalize_bank_ids_and_keep_source_rows(ibm_dir: Path) -> None:
    tx = ibm_aml.read_transactions(ibm_dir / ibm_aml.TRANSACTIONS_FILE)
    first = tx.row(0, named=True)
    assert first["transaction_id"] == "ibm-1"
    assert first["source_row"] == 1
    assert first["from_account_id"] == "10:A1"  # "010" in the file
    assert first["timestamp"] == datetime(2022, 9, 1, 0, 0)
    assert first["is_laundering"] is True
    assert tx.height == 26


def test_unexpected_header_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "trans.csv"
    path.write_text(IBM_HEADER.replace("Is Laundering", "Label") + "\n")
    with pytest.raises(ValueError, match="unexpected header"):
        ibm_aml.read_transactions(path)


def test_accounts_join_on_normalized_bank_ids(ibm_dir: Path) -> None:
    accounts = ibm_aml.read_accounts(ibm_dir / ibm_aml.ACCOUNTS_FILE)
    a1 = accounts.filter(pl.col("account_id") == "10:A1").row(0, named=True)
    assert a1["entity_id"] == "E1"
    assert a1["entity_type"] == "Corporation"


def test_patterns_parse_typology_and_description(ibm_dir: Path) -> None:
    patterns = ibm_aml.read_patterns(ibm_dir / ibm_aml.PATTERNS_FILE)
    assert patterns.height == 2
    assert set(patterns["pattern_id"]) == {"ibm-pattern-0001"}
    assert set(patterns["typology"]) == {"FAN-OUT"}
    assert set(patterns["pattern_description"]) == {"Max 2-degree Fan-Out"}


def test_pattern_line_outside_block_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "patterns.txt"
    path.write_text("2022/09/01 00:00,010,A1,020,B1,1,US Dollar,1,US Dollar,ACH,1\n")
    with pytest.raises(ValueError, match="outside a pattern block"):
        ibm_aml.read_patterns(path)


def test_match_patterns_picks_lowest_row_among_duplicates(ibm_dir: Path) -> None:
    tx = ibm_aml.read_transactions(ibm_dir / ibm_aml.TRANSACTIONS_FILE)
    duplicate = tx.filter(pl.col("transaction_id") == "ibm-1").with_columns(
        pl.lit("ibm-999").alias("transaction_id"), pl.lit(999, pl.Int64).alias("source_row")
    )
    patterns = ibm_aml.read_patterns(ibm_dir / ibm_aml.PATTERNS_FILE)
    matched, ambiguous = ibm_aml.match_patterns(patterns, pl.concat([tx, duplicate]))
    assert ambiguous == 1
    assert set(matched["transaction_id"]) == {"ibm-1", "ibm-2"}


def test_unmatched_pattern_line_raises(ibm_dir: Path) -> None:
    tx = ibm_aml.read_transactions(ibm_dir / ibm_aml.TRANSACTIONS_FILE)
    patterns = ibm_aml.read_patterns(ibm_dir / ibm_aml.PATTERNS_FILE)
    with pytest.raises(ValueError, match="no matching transaction"):
        ibm_aml.match_patterns(patterns, tx.filter(pl.col("transaction_id") != "ibm-2"))


def test_sample_keeps_core_drops_self_transfers_and_caps_hubs(ibm_dir: Path) -> None:
    config = fixture_settings(ibm_dir.parent.parent, ibm_benign_seed_accounts=0)
    tx = ibm_aml.read_transactions(ibm_dir / ibm_aml.TRANSACTIONS_FILE)
    core = pl.Series(["ibm-1", "ibm-2", "ibm-3"])
    sample = ibm_aml.sample_transactions(tx, core, config)

    assert set(core) <= set(sample["transaction_id"])
    assert "ibm-4" not in set(sample["transaction_id"])  # self-transfer, not core
    # H1 is not a seed, so its 20 cheques only enter through seed D1 (capped at 5 per seed).
    assert sample.filter(pl.col("from_account_id") == "40:H1").height == 0
    per_seed = (
        pl.concat([sample.select(a="from_account_id"), sample.select(a="to_account_id")])
        .group_by("a")
        .len()
    )
    non_core = sample.filter(~pl.col("transaction_id").is_in(core.implode()))
    assert non_core.height <= 5 * per_seed.height


def test_hub_seed_is_capped(ibm_dir: Path) -> None:
    config = fixture_settings(ibm_dir.parent.parent, ibm_benign_seed_accounts=0)
    tx = ibm_aml.read_transactions(ibm_dir / ibm_aml.TRANSACTIONS_FILE)
    hub_core = pl.Series(["ibm-25"])  # D1 -> H1 makes H1 a seed
    sample = ibm_aml.sample_transactions(tx, hub_core, config)
    assert sample.filter(pl.col("from_account_id") == "40:H1").height == 5


def test_sampling_is_deterministic(ibm_dir: Path) -> None:
    config = fixture_settings(ibm_dir.parent.parent)
    tx = ibm_aml.read_transactions(ibm_dir / ibm_aml.TRANSACTIONS_FILE)
    core = pl.Series(["ibm-1"])
    first = ibm_aml.sample_transactions(tx, core, config)
    second = ibm_aml.sample_transactions(tx, core, config)
    assert first.equals(second)


def test_sample_above_global_cap_raises_instead_of_truncating(ibm_dir: Path) -> None:
    config = fixture_settings(ibm_dir.parent.parent, ibm_max_transactions=2)
    tx = ibm_aml.read_transactions(ibm_dir / ibm_aml.TRANSACTIONS_FILE)
    with pytest.raises(ValueError, match="instead of truncating"):
        ibm_aml.sample_transactions(tx, pl.Series(["ibm-1", "ibm-2", "ibm-3"]), config)


def test_stage_separates_labels_from_staging_tables(ibm_dir: Path) -> None:
    stage = ibm_aml.build_ibm_stage(ibm_dir, fixture_settings(ibm_dir.parent.parent))
    assert "is_laundering" not in stage.transactions.columns
    truth = stage.pattern_ground_truth
    assert set(truth["transaction_id"]) == {"ibm-1", "ibm-2", "ibm-3"}
    assert truth.filter(pl.col("pattern_id").is_null())["transaction_id"].to_list() == ["ibm-3"]
    assert {"10:A1", "20:B1", "30:C1", "10:D1"} <= stage.laundering_account_ids
    assert stage.stats["sampled_accounts_without_entity"] == 0
