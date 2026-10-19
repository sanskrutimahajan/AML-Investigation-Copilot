"""Strict pandera schemas for every staging table (ADR 003 §1-2).

Staging tables are the only inputs graph loaders read. Schemas are strict, so any column not listed
here (in particular a laundering label or ground-truth field) fails validation.
"""

import pandera.polars as pa
import polars as pl

from tracegraph.ingest import icij, opensanctions

# Columns that hold eval answers. They must never appear in a staging table.
LABEL_COLUMNS = frozenset(
    {
        "is_laundering",
        "pattern_id",
        "typology",
        "pattern_description",
        "is_match",
        "link_type",
        "seed_type",
        "seed_reason",
    }
)


def _id(dtype: pl.DataType | type[pl.DataType], *checks: pa.Check) -> pa.Column:
    """A required, unique identifier column."""
    return pa.Column(dtype, list(checks) or None, nullable=False, unique=True)


_TEXT = pl.Utf8
_LIST = pl.List(pl.Utf8)

STAGING_SCHEMAS: dict[str, pa.DataFrameSchema] = {
    "ibm_transactions": pa.DataFrameSchema(
        {
            "transaction_id": _id(_TEXT),
            "timestamp": pa.Column(pl.Datetime("us"), nullable=False),
            "from_account_id": pa.Column(_TEXT, nullable=False),
            "to_account_id": pa.Column(_TEXT, nullable=False),
            "amount_paid": pa.Column(pl.Float64, pa.Check.ge(0), nullable=False),
            "payment_currency": pa.Column(_TEXT, nullable=False),
            "amount_received": pa.Column(pl.Float64, pa.Check.ge(0), nullable=False),
            "receiving_currency": pa.Column(_TEXT, nullable=False),
            "payment_format": pa.Column(_TEXT, nullable=False),
            "source_row": _id(pl.Int64, pa.Check.ge(1)),
        },
        strict=True,
    ),
    "ibm_accounts": pa.DataFrameSchema(
        {
            "account_id": _id(_TEXT),
            "bank_id": pa.Column(_TEXT, nullable=False),
            "account_number": pa.Column(_TEXT, nullable=False),
            "bank_name": pa.Column(_TEXT, nullable=True),
            "entity_id": pa.Column(_TEXT, nullable=True),
            "entity_type": pa.Column(_TEXT, nullable=True),
            "customer_id": pa.Column(_TEXT, nullable=True),
        },
        strict=True,
    ),
    "ibm_banks": pa.DataFrameSchema(
        {"bank_id": _id(_TEXT), "bank_name": pa.Column(_TEXT, nullable=True)},
        strict=True,
    ),
    "customers": pa.DataFrameSchema(
        {
            "customer_id": _id(_TEXT),
            "ibm_entity_id": _id(_TEXT),
            "ibm_entity_type": pa.Column(_TEXT, nullable=False),
            "customer_type": pa.Column(
                _TEXT, pa.Check.isin(["person", "organization"]), nullable=False
            ),
            "name": pa.Column(_TEXT, pa.Check.str_length(min_value=1), nullable=False),
            "date_of_birth": pa.Column(pl.Date, nullable=True),
            "incorporation_date": pa.Column(pl.Date, nullable=True),
            "country": pa.Column(_TEXT, nullable=False),
            "address": pa.Column(_TEXT, nullable=False),
            "synthetic": pa.Column(pl.Boolean, pa.Check.eq(True), nullable=False),
        },
        strict=True,
    ),
    "opensanctions_entities": pa.DataFrameSchema(
        {
            "entity_id": _id(_TEXT),
            "source_id": _id(_TEXT),
            "schema": pa.Column(
                _TEXT,
                pa.Check.isin(["Person", "Organization", "Company", "LegalEntity"]),
                nullable=False,
            ),
            "name": pa.Column(_TEXT, nullable=False),
            "aliases": pa.Column(_LIST, nullable=False),
            "birth_dates": pa.Column(_LIST, nullable=False),
            "countries": pa.Column(_LIST, nullable=False),
            "topics": pa.Column(_LIST, nullable=False),
            "datasets": pa.Column(_LIST, nullable=False),
        },
        strict=True,
    ),
    "opensanctions_relationships": pa.DataFrameSchema(
        {
            "relationship_id": _id(_TEXT),
            "rel_type": pa.Column(
                _TEXT, pa.Check.isin(sorted(opensanctions.REL_TYPES)), nullable=False
            ),
            "start_id": pa.Column(_TEXT, nullable=False),
            "end_id": pa.Column(_TEXT, nullable=False),
            "role": pa.Column(_TEXT, nullable=True),
            "datasets": pa.Column(_LIST, nullable=False),
        },
        strict=True,
    ),
    "icij_nodes": pa.DataFrameSchema(
        {
            "entity_id": _id(_TEXT),
            "source_id": _id(_TEXT),
            "node_type": pa.Column(
                _TEXT,
                pa.Check.isin(["entity", "officer", "intermediary", "address", "other"]),
                nullable=False,
            ),
            "name": pa.Column(_TEXT, nullable=True),
            "jurisdiction": pa.Column(_TEXT, nullable=True),
            "jurisdiction_description": pa.Column(_TEXT, nullable=True),
            "company_type": pa.Column(_TEXT, nullable=True),
            "incorporation_date": pa.Column(_TEXT, nullable=True),
            "status": pa.Column(_TEXT, nullable=True),
            "address": pa.Column(_TEXT, nullable=True),
            "country_codes": pa.Column(_TEXT, nullable=True),
            "countries": pa.Column(_TEXT, nullable=True),
            "source_leak": pa.Column(_TEXT, nullable=False),
        },
        strict=True,
    ),
    "icij_relationships": pa.DataFrameSchema(
        {
            "relationship_id": _id(_TEXT),
            "start_id": pa.Column(_TEXT, nullable=False),
            "end_id": pa.Column(_TEXT, nullable=False),
            "rel_type": pa.Column(_TEXT, pa.Check.isin(sorted(icij.REL_TYPES)), nullable=False),
            "link": pa.Column(_TEXT, nullable=True),
            "start_date": pa.Column(_TEXT, nullable=True),
            "end_date": pa.Column(_TEXT, nullable=True),
            "source_leak": pa.Column(_TEXT, nullable=True),
        },
        strict=True,
    ),
}


def validate(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    """Validate a staging table, collecting every failure rather than stopping at the first."""
    leaked = LABEL_COLUMNS & set(frame.columns)
    if leaked:
        raise ValueError(f"staging table {name!r} contains label columns {sorted(leaked)}")
    return STAGING_SCHEMAS[name].validate(frame, lazy=True)
