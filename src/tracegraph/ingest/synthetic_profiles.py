"""Synthetic customer schema and ownership: one `Customer` per IBM entity in the sample.

Customers are built by `seed_links.build_customers`, which gives plain, seeded and look-alike
customers the same attribute procedure (`customer_attributes.py`, ADR 006). Every row has
`synthetic=True`.
"""

import polars as pl

# IBM entity types that represent a natural person; every other type becomes an organization.
PERSON_ENTITY_TYPES = frozenset({"Individual", "Sole Proprietorship"})

CUSTOMER_SCHEMA: dict[str, pl.DataType] = {
    "customer_id": pl.Utf8(),
    "ibm_entity_id": pl.Utf8(),
    "ibm_entity_type": pl.Utf8(),
    "customer_type": pl.Utf8(),
    "name": pl.Utf8(),
    "date_of_birth": pl.Date(),
    "incorporation_date": pl.Date(),
    "country": pl.Utf8(),
    "address": pl.Utf8(),
    "synthetic": pl.Boolean(),
}


def customer_id(ibm_entity_id: str) -> str:
    return f"cust-{ibm_entity_id}"


def attach_customers(accounts: pl.DataFrame) -> pl.DataFrame:
    """Add the owning `customer_id` to each account (the OWNS relationship source)."""
    return accounts.with_columns(
        pl.when(pl.col("entity_id").is_not_null())
        .then(pl.format("cust-{}", "entity_id"))
        .otherwise(None)
        .alias("customer_id")
    )
