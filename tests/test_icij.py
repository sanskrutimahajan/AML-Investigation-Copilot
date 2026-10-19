"""ICIJ node/relationship parsing and bounded subgraph sampling."""

from pathlib import Path

import polars as pl
import pytest
from raw_fixtures import fixture_settings, write_icij

from tracegraph.ingest import icij


@pytest.fixture
def icij_dir(tmp_path: Path) -> Path:
    return write_icij(tmp_path / "raw")


def test_nodes_are_unioned_with_types(icij_dir: Path) -> None:
    nodes = icij.read_nodes(icij_dir / "csv")
    types = dict(nodes.select("entity_id", "node_type").iter_rows())
    assert types["icij:1"] == "entity"
    assert types["icij:10"] == "officer"
    assert types["icij:20"] == "intermediary"
    assert types["icij:30"] == "address"
    address = nodes.filter(pl.col("entity_id") == "icij:30").row(0, named=True)
    assert address["name"] == "1 Harbour Rd Hong Kong"  # address text fills the empty name


def test_icij_resolution_links_are_renamed(icij_dir: Path) -> None:
    rel_types = set(icij.read_relationships(icij_dir / "csv")["rel_type"])
    assert {"ICIJ_SAME_AS", "ICIJ_SIMILAR", "OFFICER_OF"} <= rel_types
    assert "SAME_AS" not in rel_types


def test_subgraph_stays_in_scope_and_keeps_only_internal_edges(icij_dir: Path) -> None:
    config = fixture_settings(icij_dir.parent.parent, icij_seed_entity_count=6)
    stage = icij.build_icij_stage(icij_dir, config)
    ids = set(stage.nodes["entity_id"])
    assert "icij:7" not in ids  # Offshore Leaks is out of scope
    assert set(stage.nodes["source_leak"]) == {"Panama Papers"}
    for row in stage.relationships.iter_rows(named=True):
        assert {row["start_id"], row["end_id"]} <= ids
    assert stage.relationships["relationship_id"].n_unique() == stage.relationships.height


def test_second_hop_goes_through_officers(icij_dir: Path) -> None:
    nodes, rels = icij.read_nodes(icij_dir / "csv"), icij.read_relationships(icij_dir / "csv")
    config = fixture_settings(icij_dir.parent.parent, icij_seed_entity_count=6)
    full_nodes, _ = icij.sample_subgraph(nodes, rels, config)
    # With every entity seeded, officers 10-12 and their companies are all included.
    assert {"icij:10", "icij:11", "icij:12"} <= set(full_nodes["entity_id"])


def test_hop1_above_node_cap_raises(icij_dir: Path) -> None:
    config = fixture_settings(
        icij_dir.parent.parent, icij_seed_entity_count=6, icij_node_sample_size=3
    )
    with pytest.raises(ValueError, match="lower icij_seed_entity_count"):
        icij.build_icij_stage(icij_dir, config)


def test_sampling_is_deterministic(icij_dir: Path) -> None:
    config = fixture_settings(icij_dir.parent.parent)
    first, second = icij.build_icij_stage(icij_dir, config), icij.build_icij_stage(icij_dir, config)
    assert first.nodes.equals(second.nodes)
    assert first.relationships.equals(second.relationships)
