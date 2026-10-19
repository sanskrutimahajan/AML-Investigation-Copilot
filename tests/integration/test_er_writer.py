"""ER edges in the throwaway Neo4j: thresholds, loud failure on rerun, reset scope."""

import polars as pl
import pytest
from neo4j import Driver

from tests.integration.conftest import DATABASE
from tracegraph.config import Settings
from tracegraph.er import writer
from tracegraph.graph import loaders

pytestmark = pytest.mark.integration


def _predictions(staged: Settings) -> pl.DataFrame:
    customers = pl.read_parquet(staged.staging_dir / "customers.parquet")["customer_id"]
    entities = pl.read_parquet(staged.staging_dir / "opensanctions_entities.parquet")["entity_id"]
    return pl.DataFrame(
        {
            "customer_id": [customers[0], customers[1], customers[2]],
            "entity_id": [entities[0], entities[1], entities[2]],
            "probability": [0.99, 0.80, 0.10],
            "match_weight": [10.0, 2.0, -5.0],
            "gamma_name": [4, 2, 0],
        }
    )


def _write(driver: Driver, staged: Settings, run_id: str) -> dict[str, int]:
    return writer.write_er_edges(
        driver,
        DATABASE,
        _predictions(staged),
        match_threshold=0.92,
        review_threshold=0.75,
        run_id=run_id,
        method="test",
        batch_size=2,
    )


def test_edges_follow_thresholds_and_rerun_fails_loudly(
    empty_graph: Driver, staged: Settings
) -> None:
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    assert _write(empty_graph, staged, "run-1") == {"SAME_AS": 1, "POSSIBLE_SAME_AS": 1}
    records, _, _ = empty_graph.execute_query(
        "MATCH (:Customer)-[r:SAME_AS]->(:SourceEntity) RETURN r", database_=DATABASE
    )
    edge = records[0]["r"]
    assert edge["synthetic_link"] is True and edge["er_run_id"] == "run-1"
    assert edge["probability"] == 0.99 and '"name": 4' in edge["evidence"]

    with pytest.raises(writer.ExistingERRunError, match="make er-reset"):
        _write(empty_graph, staged, "run-2")
    assert writer.er_edge_counts(empty_graph, DATABASE) == {"SAME_AS": 1, "POSSIBLE_SAME_AS": 1}


def test_reset_removes_only_er_edges_and_reload_still_verifies(
    empty_graph: Driver, staged: Settings
) -> None:
    report = loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    _write(empty_graph, staged, "run-1")
    # A repeat load with ER edges present still verifies (ER edges are not staging data).
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    assert writer.reset_er_edges(empty_graph, DATABASE) == 2
    labels, rels = loaders.graph_counts(empty_graph, DATABASE)
    assert labels == report.node_labels and rels == report.relationship_types
