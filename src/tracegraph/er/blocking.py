"""ER records and blocking keys (ADR 007).

Builds one table of customers and one of source entities (OpenSanctions parties, ICIJ officers,
entities, intermediaries and others; ICIJ addresses are not parties). Both tables share the same
columns, and every pair comparison is symmetric. Splink link-only mode may put either side on the
left of a pair.

Blocking keys (all combined with an equal `kind`, person or organization):
- `rare_tokens`: name tokens whose document frequency across both tables is at most
  `er_block_max_token_frequency`, so words like "trading" or "limited" never form a block.
- `phon_keys`: Metaphone keys of rare tokens, which catch typos and transliterations.
- `ann_ids`: embedding nearest neighbours, filled in by `embeddings.py`.

Reads staging only. Never reads ground truth.
"""

from collections import Counter
from pathlib import Path

import polars as pl

from tracegraph.er.normalize import (
    birth_year,
    name_tokens,
    normalize_name,
    parse_date,
    phonetic_key,
    split_legal_suffix,
)
from tracegraph.ingest.customer_attributes import iso2
from tracegraph.ingest.seed_links import looks_like_organization

RECORD_SCHEMA: dict[str, pl.DataType] = {
    "unique_id": pl.Utf8(),
    "kind": pl.Utf8(),
    "name_raw": pl.Utf8(),
    "name_norm": pl.Utf8(),
    "names": pl.List(pl.Utf8),
    "tokens": pl.List(pl.Utf8),
    "suffix_class": pl.Utf8(),
    "dob": pl.Utf8(),
    "dob_year": pl.Int64(),
    "country": pl.Utf8(),
}
# Fallback blocking token is used only if it is not extremely common.
_FALLBACK_FREQUENCY_FACTOR = 10


def _record(
    unique_id: str,
    kind: str,
    name: str,
    aliases: list[str],
    dob: object,
    country: str | None,
) -> dict[str, object] | None:
    primary = normalize_name(name)
    if not primary:
        return None
    variants = [primary, *(normalize_name(a) for a in aliases)]
    names = list(dict.fromkeys(v for v in variants if v))
    cores = [split_legal_suffix(n)[0] for n in names]
    parsed = parse_date(dob if isinstance(dob, str) or dob is None else dob)  # type: ignore[arg-type]
    return {
        "unique_id": unique_id,
        "kind": kind,
        "name_raw": name,
        "name_norm": primary,
        "names": names,
        "tokens": sorted({t for core in cores for t in name_tokens(core)}),
        "suffix_class": split_legal_suffix(primary)[1],
        "dob": parsed.isoformat() if parsed else None,
        "dob_year": birth_year(dob if isinstance(dob, str) or dob is None else dob),  # type: ignore[arg-type]
        "country": country,
    }


def customer_records(customers: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for row in customers.iter_rows(named=True):
        record = _record(
            row["customer_id"],
            row["customer_type"],
            row["name"],
            [],
            row["date_of_birth"],
            row["country"],
        )
        if record:
            rows.append(record)
    return pl.DataFrame(rows, schema=RECORD_SCHEMA)


def source_records(opensanctions: pl.DataFrame, icij: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for row in opensanctions.iter_rows(named=True):
        dob = next(
            (d for d in row["birth_dates"] if parse_date(d)), next(iter(row["birth_dates"]), None)
        )
        record = _record(
            row["entity_id"],
            "person" if row["schema"] == "Person" else "organization",
            row["name"],
            row["aliases"],
            dob,
            next((c for c in map(iso2, row["countries"]) if c), None),
        )
        if record:
            rows.append(record)
    parties = icij.filter(pl.col("node_type") != "address")
    for row in parties.iter_rows(named=True):
        if not row["name"]:
            continue
        person = row["node_type"] == "officer" and not looks_like_organization(row["name"])
        record = _record(
            row["entity_id"],
            "person" if person else "organization",
            row["name"],
            [],
            None,
            iso2(row["country_codes"]),
        )
        if record:
            rows.append(record)
    return pl.DataFrame(rows, schema=RECORD_SCHEMA)


def add_blocking_keys(
    customers: pl.DataFrame, sources: pl.DataFrame, max_frequency: int
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Add `rare_tokens`, `phon_keys` and `first_block_key` (for EM training) to both tables."""
    frequency: Counter[str] = Counter()
    for frame in (customers, sources):
        for tokens in frame["tokens"]:
            frequency.update(tokens)
    phonetic_frequency: Counter[str] = Counter()

    def rare(tokens: list[str]) -> list[str]:
        keep = [t for t in tokens if len(t) >= 3 and frequency[t] <= max_frequency]
        if keep:
            return keep
        candidates = [t for t in tokens if len(t) >= 3]
        if candidates:
            best = min(candidates, key=lambda t: (frequency[t], t))
            if frequency[best] <= max_frequency * _FALLBACK_FREQUENCY_FACTOR:
                return [best]
        return []

    rare_lists = [[rare(list(t)) for t in frame["tokens"]] for frame in (customers, sources)]
    for lists in rare_lists:
        for tokens in lists:
            phonetic_frequency.update({phonetic_key(t) for t in tokens if len(t) >= 4})

    def phonetic(tokens: list[str]) -> list[str]:
        keys = {phonetic_key(t) for t in tokens if len(t) >= 4}
        return sorted(k for k in keys if k and phonetic_frequency[k] <= max_frequency)

    out = []
    for frame, lists in zip((customers, sources), rare_lists, strict=True):
        out.append(
            frame.with_columns(
                pl.Series("rare_tokens", lists, dtype=pl.List(pl.Utf8)),
                pl.Series("phon_keys", [phonetic(t) for t in lists], dtype=pl.List(pl.Utf8)),
                pl.Series(
                    "first_block_key",
                    [min(t, key=lambda x: (frequency[x], x)) if t else None for t in lists],
                    dtype=pl.Utf8,
                ),
            )
        )
    return out[0], out[1]


def build_records(staging_dir: Path, max_frequency: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    customers = customer_records(pl.read_parquet(staging_dir / "customers.parquet"))
    sources = source_records(
        pl.read_parquet(staging_dir / "opensanctions_entities.parquet"),
        pl.read_parquet(staging_dir / "icij_nodes.parquet"),
    )
    return add_blocking_keys(customers, sources, max_frequency)


def candidate_pairs(customers: pl.DataFrame, sources: pl.DataFrame) -> pl.DataFrame:
    """Every (customer_id, entity_id) pair a blocking key produces, plus the keys that found it."""
    frames = []
    for key in ("rare_tokens", "phon_keys", "ann_ids"):
        if key not in customers.columns:
            continue
        left = customers.select("unique_id", "kind", key).explode(key).drop_nulls(key)
        right = sources.select("unique_id", "kind", key).explode(key).drop_nulls(key)
        frames.append(
            left.join(right, on=["kind", key], suffix="_r")
            .select(customer_id="unique_id", entity_id="unique_id_r")
            .unique()
            .with_columns(pl.lit(key).alias("found_by"))
        )
    pairs = pl.concat(frames)
    return pairs.group_by("customer_id", "entity_id").agg(pl.col("found_by").sort())
