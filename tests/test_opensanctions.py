"""OpenSanctions FtM parsing, merging, and relationship filtering."""

from pathlib import Path

import polars as pl
import pytest
from raw_fixtures import fixture_settings, write_opensanctions

from tracegraph.ingest import opensanctions


@pytest.fixture
def os_dir(tmp_path: Path) -> Path:
    return write_opensanctions(tmp_path / "raw")


def test_only_named_parties_are_kept(os_dir: Path) -> None:
    stage = opensanctions.build_opensanctions_stage(os_dir, fixture_settings(os_dir.parent.parent))
    ids = set(stage.entities["source_id"])
    assert ids == {"NK-p1", "NK-o1", "NK-p2", "NK-c1"}  # no Address, no nameless Person


def test_entities_in_several_lists_are_merged(os_dir: Path) -> None:
    stage = opensanctions.build_opensanctions_stage(os_dir, fixture_settings(os_dir.parent.parent))
    p1 = stage.entities.filter(pl.col("source_id") == "NK-p1").row(0, named=True)
    assert p1["entity_id"] == "opensanctions:NK-p1"
    assert p1["datasets"] == ["list_a", "list_b"]
    assert p1["aliases"] == ["M. Y. Khan", "Abu Yusuf"]
    assert p1["countries"] == ["pk", "af"]
    assert p1["birth_dates"] == ["1970-02-03"]


def test_relationships_need_both_endpoints(os_dir: Path) -> None:
    stage = opensanctions.build_opensanctions_stage(os_dir, fixture_settings(os_dir.parent.parent))
    rels = {(r["rel_type"], r["start_id"], r["end_id"]) for r in stage.relationships.to_dicts()}
    assert rels == {
        ("OWNER_OF", "opensanctions:NK-p1", "opensanctions:NK-o1"),
        ("DIRECTOR_OF", "opensanctions:NK-p2", "opensanctions:NK-c1"),
    }
    assert stage.stats["parsed_relationships"] == 3  # own-2 points at an Address


def test_entity_cap_is_deterministic_and_filters_relationships(os_dir: Path) -> None:
    config = fixture_settings(os_dir.parent.parent, opensanctions_entity_sample_size=2)
    first = opensanctions.build_opensanctions_stage(os_dir, config)
    second = opensanctions.build_opensanctions_stage(os_dir, config)
    assert first.entities.height == 2
    assert first.entities.equals(second.entities)
    kept = set(first.entities["entity_id"])
    for row in first.relationships.iter_rows(named=True):
        assert {row["start_id"], row["end_id"]} <= kept


def test_invalid_json_line_reports_location(tmp_path: Path) -> None:
    path = tmp_path / "bad.entities.ftm.json"
    path.write_text('{"id": "x"}\nnot json\n')
    with pytest.raises(ValueError, match="bad.entities.ftm.json:2"):
        list(opensanctions.iter_ftm(path))
