"""Loading staged fixture data into the throwaway Neo4j (ADR 003)."""

import polars as pl
import pytest
from neo4j import Driver

from tests.integration.conftest import DATABASE
from tracegraph.config import Settings
from tracegraph.graph import loaders
from tracegraph.graph.reset import reset_graph
from tracegraph.graph.schema import apply_schema, schema_statements
from tracegraph.ingest.staging_schemas import LABEL_COLUMNS

pytestmark = pytest.mark.integration


def _load(driver: Driver, config: Settings) -> loaders.LoadReport:
    return loaders.load_graph(driver, DATABASE, config.staging_dir, batch_size=7)


def _scalar(driver: Driver, query: str) -> int:
    records, _, _ = driver.execute_query(query, database_=DATABASE)
    return int(records[0][0])


def test_schema_is_idempotent(empty_graph: Driver) -> None:
    apply_schema(empty_graph, DATABASE)
    first = _scalar(empty_graph, "SHOW CONSTRAINTS YIELD name RETURN count(*)")
    apply_schema(empty_graph, DATABASE)
    assert _scalar(empty_graph, "SHOW CONSTRAINTS YIELD name RETURN count(*)") == first
    assert first == sum(s.startswith("CREATE CONSTRAINT") for s in schema_statements())


def test_load_matches_staging_and_is_idempotent(empty_graph: Driver, staged: Settings) -> None:
    first = _load(empty_graph, staged)
    assert first.node_labels == first.expected_node_labels
    assert first.relationship_types == first.expected_relationship_types
    sent = pl.read_parquet(staged.staging_dir / "ibm_transactions.parquet").height
    assert first.relationship_types["SENT"] == sent

    second = _load(empty_graph, staged)  # same stage again: nothing new, nothing duplicated
    assert second.node_labels == first.node_labels
    assert second.relationship_types == first.relationship_types


def test_sent_edges_carry_no_labels(empty_graph: Driver, staged: Settings) -> None:
    _load(empty_graph, staged)
    records, _, _ = empty_graph.execute_query(
        "MATCH ()-[r:SENT]->() UNWIND keys(r) AS key RETURN collect(DISTINCT key) AS keys",
        database_=DATABASE,
    )
    keys = set(records[0]["keys"])
    assert keys == set(loaders.SENT_PROPERTIES)
    assert not keys & LABEL_COLUMNS
    assert _scalar(empty_graph, "MATCH ()-[r:SAME_AS]->() RETURN count(r)") == 0


def test_stale_graph_fails_loudly_without_deleting(empty_graph: Driver, staged: Settings) -> None:
    _load(empty_graph, staged)
    empty_graph.execute_query("CREATE (:Account {account_id: 'stale:1'})", database_=DATABASE)
    before = _scalar(empty_graph, "MATCH (n) RETURN count(n)")
    with pytest.raises(loaders.StagingMismatchError, match="make graph-reset"):
        _load(empty_graph, staged)
    assert _scalar(empty_graph, "MATCH (n) RETURN count(n)") == before


def test_invalid_staging_is_refused_before_writing(
    empty_graph: Driver, staged: Settings, tmp_path_factory: pytest.TempPathFactory
) -> None:
    bad_dir = tmp_path_factory.mktemp("bad_staging")
    for path in staged.staging_dir.glob("*.parquet"):
        frame = pl.read_parquet(path)
        if path.stem == "ibm_transactions":
            frame = frame.with_columns(pl.lit(False).alias("is_laundering"))
        frame.write_parquet(bad_dir / path.name)
    with pytest.raises(ValueError, match="label columns"):
        loaders.load_graph(empty_graph, DATABASE, bad_dir, batch_size=7)
    assert _scalar(empty_graph, "MATCH (n) RETURN count(n)") == 0


def test_reset_deletes_everything(empty_graph: Driver, staged: Settings) -> None:
    report = _load(empty_graph, staged)
    assert reset_graph(empty_graph, DATABASE) == report.total_nodes
    assert _scalar(empty_graph, "MATCH (n) RETURN count(n)") == 0
