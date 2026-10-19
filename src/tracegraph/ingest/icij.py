"""ICIJ Offshore Leaks CSV export: a bounded, connected subgraph of selected leaks.

Source: https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip
License: database ODbL, contents CC BY-SA; cite the International Consortium of Investigative
Journalists. Inclusion in the database does not imply wrongdoing.
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from tracegraph.config import Settings

NODE_FILES: dict[str, str] = {
    "entity": "nodes-entities.csv",
    "officer": "nodes-officers.csv",
    "intermediary": "nodes-intermediaries.csv",
    "address": "nodes-addresses.csv",
    "other": "nodes-others.csv",
}
RELATIONSHIPS_FILE = "relationships.csv"
RELATIONSHIPS_HEADER = [
    "node_id_start",
    "node_id_end",
    "rel_type",
    "link",
    "status",
    "start_date",
    "end_date",
    "sourceID",
]
# Columns every node file must have; the remaining staged columns are optional per file.
REQUIRED_NODE_COLUMNS = ["node_id", "name", "countries", "country_codes", "sourceID"]
NODE_COLUMNS = [
    "jurisdiction",
    "jurisdiction_description",
    "company_type",
    "incorporation_date",
    "status",
    "address",
]
# ICIJ's own "possibly the same" links must not be confused with our entity-resolution SAME_AS.
_RENAMED_REL_TYPES = {
    "same_as": "ICIJ_SAME_AS",
    "similar": "ICIJ_SIMILAR",
    "same_name_as": "ICIJ_SAME_NAME_AS",
    "same_company_as": "ICIJ_SAME_COMPANY_AS",
    "same_intermediary_as": "ICIJ_SAME_INTERMEDIARY_AS",
    "same_id_as": "ICIJ_SAME_ID_AS",
    "similar_company_as": "ICIJ_SIMILAR_COMPANY_AS",
    "probably_same_officer_as": "ICIJ_PROBABLY_SAME_OFFICER_AS",
    "same_address_as": "ICIJ_SAME_ADDRESS_AS",
}
# Every relationship type the graph loader accepts from ICIJ (observed in the 2026-09-09 export).
REL_TYPES = frozenset(
    {"OFFICER_OF", "REGISTERED_ADDRESS", "INTERMEDIARY_OF", "CONNECTED_TO", "UNDERLYING"}
    | set(_RENAMED_REL_TYPES.values())
)


@dataclass(frozen=True)
class IcijStage:
    nodes: pl.DataFrame
    relationships: pl.DataFrame
    stats: dict[str, int]


def _header(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as handle:
        return handle.readline().rstrip("\r\n").split(",")


def read_nodes(csv_dir: Path) -> pl.DataFrame:
    """Union all node files into one table; `address` nodes keep their text in `address`."""
    frames = []
    for node_type, filename in NODE_FILES.items():
        path = csv_dir / filename
        header = _header(path)
        missing = [c for c in REQUIRED_NODE_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"{filename}: missing columns {missing}")
        frame = pl.read_csv(path, infer_schema=False)
        if node_type == "address":
            # Address nodes carry the text in "address"; "name" is mostly empty.
            frame = frame.with_columns(pl.coalesce("name", "address").alias("name"))
        frames.append(
            frame.select(
                pl.format("icij:{}", "node_id").alias("entity_id"),
                pl.col("node_id").alias("source_id"),
                pl.lit(node_type).alias("node_type"),
                pl.col("name").str.strip_chars(),
                *(
                    pl.col(c) if c in frame.columns else pl.lit(None, pl.Utf8).alias(c)
                    for c in NODE_COLUMNS
                ),
                pl.col("country_codes"),
                pl.col("countries"),
                pl.col("sourceID").alias("source_leak"),
            )
        )
    return pl.concat(frames).unique("entity_id", keep="first", maintain_order=True)


def read_relationships(csv_dir: Path) -> pl.DataFrame:
    path = csv_dir / RELATIONSHIPS_FILE
    if _header(path) != RELATIONSHIPS_HEADER:
        raise ValueError(f"{RELATIONSHIPS_FILE}: unexpected header {_header(path)}")
    raw = pl.read_csv(path, infer_schema=False)
    return raw.select(
        pl.format("icij:{}", "node_id_start").alias("start_id"),
        pl.format("icij:{}", "node_id_end").alias("end_id"),
        pl.col("rel_type")
        .replace_strict(_RENAMED_REL_TYPES, default=pl.col("rel_type").str.to_uppercase())
        .alias("rel_type"),
        pl.col("link"),
        pl.col("start_date"),
        pl.col("end_date"),
        pl.col("sourceID").alias("source_leak"),
    )


def sample_subgraph(
    nodes: pl.DataFrame, relationships: pl.DataFrame, config: Settings
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Seed entities -> 1-hop neighbours -> other entities of those officers, within caps."""
    rng = np.random.default_rng(config.random_seed)
    scoped = nodes.filter(pl.col("source_leak").is_in(config.icij_source_ids))
    in_scope = scoped["entity_id"].implode()
    rels = relationships.filter(
        pl.col("start_id").is_in(in_scope) & pl.col("end_id").is_in(in_scope)
    )

    entities = scoped.filter(pl.col("node_type") == "entity")["entity_id"].sort()
    n_seeds = min(config.icij_seed_entity_count, entities.len())
    seeds = entities.gather(np.sort(rng.choice(entities.len(), size=n_seeds, replace=False)))

    def neighbours(ids: pl.Series, frame: pl.DataFrame) -> pl.Series:
        id_list = ids.implode()
        return pl.concat(
            [
                frame.filter(pl.col("start_id").is_in(id_list))["end_id"],
                frame.filter(pl.col("end_id").is_in(id_list))["start_id"],
            ]
        ).unique()

    hop1 = pl.concat([seeds, neighbours(seeds, rels)]).unique()
    if hop1.len() > config.icij_node_sample_size:
        raise ValueError(
            f"ICIJ seeds + 1-hop neighbours = {hop1.len()} nodes, above icij_node_sample_size="
            f"{config.icij_node_sample_size}; lower icij_seed_entity_count."
        )
    officers = scoped.filter(
        (pl.col("node_type") == "officer") & pl.col("entity_id").is_in(hop1.implode())
    )["entity_id"]
    officer_rels = rels.filter(pl.col("rel_type") == "OFFICER_OF")
    hop2 = neighbours(officers, officer_rels)
    hop2 = hop2.filter(~hop2.is_in(hop1.implode())).sort()
    room = config.icij_node_sample_size - hop1.len()
    if hop2.len() > room:
        hop2 = hop2.gather(np.sort(rng.choice(hop2.len(), size=room, replace=False)))

    selected = pl.concat([hop1, hop2]).implode()
    sampled_nodes = scoped.filter(pl.col("entity_id").is_in(selected)).sort("entity_id")
    sampled_rels = (
        rels.filter(pl.col("start_id").is_in(selected) & pl.col("end_id").is_in(selected))
        .unique(["start_id", "end_id", "rel_type"], keep="first", maintain_order=True)
        .sort("start_id", "end_id", "rel_type")
        .with_columns(
            pl.format("icij-rel-{}", pl.int_range(1, pl.len() + 1)).alias("relationship_id")
        )
    )
    if sampled_rels.height > config.icij_relationship_sample_size:
        raise ValueError(
            f"ICIJ sample has {sampled_rels.height} relationships, above "
            f"icij_relationship_sample_size={config.icij_relationship_sample_size}."
        )
    return sampled_nodes, sampled_rels.select("relationship_id", *relationships.columns)


def build_icij_stage(raw_dir: Path, config: Settings) -> IcijStage:
    csv_dir = raw_dir / "csv"
    nodes, relationships = read_nodes(csv_dir), read_relationships(csv_dir)
    sampled_nodes, sampled_rels = sample_subgraph(nodes, relationships, config)
    stats = {
        "source_nodes": nodes.height,
        "source_relationships": relationships.height,
        "staged_nodes": sampled_nodes.height,
        "staged_relationships": sampled_rels.height,
    }
    return IcijStage(nodes=sampled_nodes, relationships=sampled_rels, stats=stats)
