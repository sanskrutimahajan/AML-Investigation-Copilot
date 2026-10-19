"""Synthetic customer profiles: one `Customer` per IBM entity in the sample.

All attributes are Faker-generated (no real personal data) and every row has `synthetic=True`.
Seeded names derived from watchlist/offshore records are applied later by `seed_links.py`.
"""

from datetime import date

import numpy as np
import polars as pl
from faker import Faker

from tracegraph.config import Settings

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


def generate_customers(accounts: pl.DataFrame, config: Settings) -> pl.DataFrame:
    """Return one deterministic synthetic profile per distinct IBM entity in `accounts`."""
    entities = (
        accounts.filter(pl.col("entity_id").is_not_null())
        .select("entity_id", "entity_type")
        .unique("entity_id", keep="first")
        .sort("entity_id")
    )
    rng = np.random.default_rng(config.random_seed)
    fakers = {}
    for index, locale in enumerate(config.faker_locales):
        fake = Faker(locale)
        fake.seed_instance(config.random_seed + index)
        fakers[locale] = fake
    locales = rng.choice(config.faker_locales, size=entities.height)

    rows = []
    for (entity_id, entity_type), locale in zip(entities.iter_rows(), locales, strict=True):
        fake = fakers[str(locale)]
        is_person = entity_type in PERSON_ENTITY_TYPES
        rows.append(
            {
                "customer_id": customer_id(entity_id),
                "ibm_entity_id": entity_id,
                "ibm_entity_type": entity_type,
                "customer_type": "person" if is_person else "organization",
                "name": fake.name() if is_person else fake.company(),
                "date_of_birth": fake.date_of_birth(minimum_age=18, maximum_age=90)
                if is_person
                else None,
                "incorporation_date": None
                if is_person
                else fake.date_between(date(1950, 1, 1), date(2021, 12, 31)),
                "country": str(locale).split("_")[1],
                "address": fake.address().replace("\n", ", "),
                "synthetic": True,
            }
        )
    return pl.DataFrame(rows, schema=CUSTOMER_SCHEMA)


def attach_customers(accounts: pl.DataFrame) -> pl.DataFrame:
    """Add the owning `customer_id` to each account (the OWNS relationship source)."""
    return accounts.with_columns(
        pl.when(pl.col("entity_id").is_not_null())
        .then(pl.format("cust-{}", "entity_id"))
        .otherwise(None)
        .alias("customer_id")
    )
