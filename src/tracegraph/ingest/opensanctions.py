"""OpenSanctions FollowTheMoney (FtM) entity exports: parties and their relationships.

Source: https://data.opensanctions.org/datasets/latest/<dataset>/entities.ftm.json
License: CC BY-NC 4.0, non-commercial use only (see docs/DATA_LICENSES.md).
Listing on a sanctions list is a legal designation; it is not a finding about this project's data.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from tracegraph.config import Settings

PARTY_SCHEMAS = frozenset({"Person", "Organization", "Company", "LegalEntity"})
# FtM relationship schema -> (start property, end property, graph relationship type)
RELATIONSHIP_SCHEMAS: dict[str, tuple[str, str, str]] = {
    "Ownership": ("owner", "asset", "OWNER_OF"),
    "Directorship": ("director", "organization", "DIRECTOR_OF"),
    "Representation": ("agent", "client", "REPRESENTS"),
    "UnknownLink": ("subject", "object", "LINKED_TO"),
    "Family": ("person", "relative", "FAMILY_OF"),
    "Associate": ("person", "associate", "ASSOCIATE_OF"),
    "Employment": ("employee", "employer", "EMPLOYED_BY"),
    "Membership": ("member", "organization", "MEMBER_OF"),
}
_ALIAS_PROPS = ("alias", "weakAlias", "previousName")
_COUNTRY_PROPS = ("country", "nationality", "citizenship", "jurisdiction", "mainCountry")

ENTITY_SCHEMA: dict[str, pl.DataType] = {
    "entity_id": pl.Utf8(),
    "source_id": pl.Utf8(),
    "schema": pl.Utf8(),
    "name": pl.Utf8(),
    "aliases": pl.List(pl.Utf8),
    "birth_dates": pl.List(pl.Utf8),
    "countries": pl.List(pl.Utf8),
    "topics": pl.List(pl.Utf8),
    "datasets": pl.List(pl.Utf8),
}
RELATIONSHIP_SCHEMA: dict[str, pl.DataType] = {
    "relationship_id": pl.Utf8(),
    "rel_type": pl.Utf8(),
    "start_id": pl.Utf8(),
    "end_id": pl.Utf8(),
    "role": pl.Utf8(),
    "datasets": pl.List(pl.Utf8),
}


@dataclass(frozen=True)
class OpenSanctionsStage:
    entities: pl.DataFrame
    relationships: pl.DataFrame
    stats: dict[str, int]


def entity_id(source_id: str) -> str:
    return f"opensanctions:{source_id}"


def iter_ftm(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one FtM entity per JSON line."""
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path.name}:{line_no}: invalid JSON") from exc


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v and v.strip()))


def _merge(rows: dict[str, dict[str, Any]], key: str, row: dict[str, Any]) -> None:
    """The same canonical ID can appear in several list exports; merge list properties."""
    if key not in rows:
        rows[key] = row
        return
    existing = rows[key]
    for column, value in row.items():
        if isinstance(value, list):
            existing[column] = _unique(existing[column] + value)
        elif existing[column] is None:
            existing[column] = value


def parse_files(paths: list[Path]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Parse FtM exports into (parties, relationships), before endpoint filtering and caps."""
    parties: dict[str, dict[str, Any]] = {}
    relationships: dict[str, dict[str, Any]] = {}
    for path in paths:
        for record in iter_ftm(path):
            schema, props = record["schema"], record.get("properties", {})
            datasets = record.get("datasets", [])
            if schema in PARTY_SCHEMAS:
                names = _unique(props.get("name", []))
                if not names:
                    continue
                aliases = _unique(names[1:] + [a for p in _ALIAS_PROPS for a in props.get(p, [])])
                _merge(
                    parties,
                    record["id"],
                    {
                        "entity_id": entity_id(record["id"]),
                        "source_id": record["id"],
                        "schema": schema,
                        "name": names[0],
                        "aliases": [a for a in aliases if a != names[0]],
                        "birth_dates": _unique(props.get("birthDate", [])),
                        "countries": _unique([c for p in _COUNTRY_PROPS for c in props.get(p, [])]),
                        "topics": _unique(props.get("topics", [])),
                        "datasets": _unique(datasets),
                    },
                )
            elif schema in RELATIONSHIP_SCHEMAS:
                start_prop, end_prop, rel_type = RELATIONSHIP_SCHEMAS[schema]
                starts, ends = props.get(start_prop, []), props.get(end_prop, [])
                if not starts or not ends:
                    continue
                roles = _unique(props.get("role", []) + props.get("relationship", []))
                _merge(
                    relationships,
                    record["id"],
                    {
                        "relationship_id": entity_id(record["id"]),
                        "rel_type": rel_type,
                        "start_id": entity_id(starts[0]),
                        "end_id": entity_id(ends[0]),
                        "role": roles[0] if roles else None,
                        "datasets": _unique(datasets),
                    },
                )
    entities = pl.DataFrame(list(parties.values()), schema=ENTITY_SCHEMA).sort("entity_id")
    rels = pl.DataFrame(list(relationships.values()), schema=RELATIONSHIP_SCHEMA)
    return entities, rels.sort("relationship_id")


def build_opensanctions_stage(raw_dir: Path, config: Settings) -> OpenSanctionsStage:
    paths = [raw_dir / f"{name}.entities.ftm.json" for name in config.opensanctions_datasets]
    entities, relationships = parse_files(paths)
    parsed_entities, parsed_relationships = entities.height, relationships.height

    if entities.height > config.opensanctions_entity_sample_size:
        rng = np.random.default_rng(config.random_seed)
        keep = rng.choice(
            entities.height, size=config.opensanctions_entity_sample_size, replace=False
        )
        entities = entities[np.sort(keep)]

    kept = entities["entity_id"].implode()
    relationships = relationships.filter(
        pl.col("start_id").is_in(kept)
        & pl.col("end_id").is_in(kept)
        & (pl.col("start_id") != pl.col("end_id"))
    )
    stats = {
        "parsed_entities": parsed_entities,
        "parsed_relationships": parsed_relationships,
        "staged_entities": entities.height,
        "staged_relationships": relationships.height,
    }
    return OpenSanctionsStage(entities=entities, relationships=relationships, stats=stats)
