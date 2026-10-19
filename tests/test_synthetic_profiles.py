"""Synthetic customer generation."""

import polars as pl

from tracegraph.config import Settings
from tracegraph.ingest.staging_schemas import validate
from tracegraph.ingest.synthetic_profiles import attach_customers, generate_customers

ACCOUNTS = pl.DataFrame(
    {
        "account_id": ["1:a", "1:b", "2:c", "3:d"],
        "entity_id": ["E1", "E1", "E2", None],
        "entity_type": ["Individual", "Individual", "Corporation", None],
    }
)


def test_one_synthetic_customer_per_entity() -> None:
    customers = generate_customers(ACCOUNTS, Settings(_env_file=None))
    assert customers["customer_id"].to_list() == ["cust-E1", "cust-E2"]
    assert customers["customer_type"].to_list() == ["person", "organization"]
    assert customers["synthetic"].all()
    person, org = customers.to_dicts()
    assert person["date_of_birth"] is not None and person["incorporation_date"] is None
    assert org["date_of_birth"] is None and org["incorporation_date"] is not None
    validate("customers", customers)


def test_generation_is_deterministic_per_seed() -> None:
    first = generate_customers(ACCOUNTS, Settings(_env_file=None, random_seed=7))
    second = generate_customers(ACCOUNTS, Settings(_env_file=None, random_seed=7))
    other = generate_customers(ACCOUNTS, Settings(_env_file=None, random_seed=8))
    assert first.equals(second)
    assert not first.equals(other)


def test_accounts_get_owning_customer() -> None:
    owned = attach_customers(ACCOUNTS)
    assert owned["customer_id"].to_list() == ["cust-E1", "cust-E1", "cust-E2", None]
