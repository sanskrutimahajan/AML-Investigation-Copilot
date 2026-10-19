"""Name perturbations and seeded-link ground truth."""

import numpy as np
import polars as pl
import pytest

from tracegraph.config import Settings
from tracegraph.ingest import seed_links as sl
from tracegraph.ingest.synthetic_profiles import attach_customers, generate_customers


def rng() -> np.random.Generator:
    return np.random.default_rng(0)


def _edit_distance_one(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
        swapped = len(diffs) == 2 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]
        return len(diffs) == 1 or swapped
    short, long = sorted((a, b), key=len)
    return any(long[:i] + long[i + 1 :] == short for i in range(len(long)))


def test_typo_is_a_single_edit() -> None:
    for seed in range(20):
        result = sl.typo("Sergei Petrov", np.random.default_rng(seed))
        assert result is not None and _edit_distance_one(result, "Sergei Petrov")


def test_transliteration_uses_known_variants_then_rules() -> None:
    assert sl.transliteration("Mohammed Khan", rng()) in {
        "Muhammad Khan",
        "Mohamed Khan",
        "Mohammad Khan",
    }
    assert sl.transliteration("José Núñez", rng()) == "Jose Nunez"
    assert sl.transliteration("KHALID ALI", rng()) == "HALID ALI"


def test_reorder_initials_and_suffix_removal() -> None:
    assert sl.token_reorder("Anna Maria Schmidt", rng()) in {
        "Schmidt, Anna Maria",
        "Schmidt Anna Maria",
    }
    assert sl.initials("Anna Maria Schmidt", rng()) == "A. Maria Schmidt"
    assert sl.suffix_removal("Ocean Ventures Trading LLC", rng()) == "Ocean Ventures Trading"
    assert sl.suffix_removal("HOTFOCUS CO., LTD.", rng()) == "HOTFOCUS"
    assert sl.suffix_removal("Ocean Ventures", rng()) is None


def test_placeholders_and_scripts_are_not_usable_seeds() -> None:
    assert sl.is_placeholder("-THE BEARER")
    assert sl.is_placeholder("THE BEARER (AGENT  MR. DENIS LOW)")
    assert not sl.is_placeholder("Bearer Holdings Ltd")
    assert not sl.is_usable_name("THE BEARER", person=False)
    assert not sl.is_usable_name("Иван Петров", person=True)
    assert not sl.is_usable_name("Madonna", person=True)  # people need two tokens
    assert sl.is_usable_name("Anna Schmidt", person=True)
    assert sl.looks_like_organization("MAYO NOMINEES LIMITED")


def test_perturb_always_changes_the_name_and_records_operations() -> None:
    record = sl.SourceRecord("os:1", "opensanctions", "Ocean Ventures Trading LLC", ("Ocean VT",))
    for seed in range(30):
        name, applied = sl.perturb(record, person=False, max_ops=2, rng=np.random.default_rng(seed))
        assert name != record.name
        assert 1 <= len(applied) <= 2
        assert set(applied) <= set(sl.PERTURBATIONS)


@pytest.fixture
def seeding_inputs() -> dict[str, pl.DataFrame]:
    accounts = attach_customers(
        pl.DataFrame(
            {
                "account_id": [f"1:{i}" for i in range(40)],
                "entity_id": [f"E{i:02d}" for i in range(40)],
                "entity_type": ["Individual" if i % 2 else "Corporation" for i in range(40)],
            }
        )
    )
    opensanctions = pl.DataFrame(
        {
            "entity_id": [f"opensanctions:{i}" for i in range(20)],
            "schema": ["Person" if i % 2 else "Organization" for i in range(20)],
            "name": [
                f"Mohammed Khan{chr(97 + i)}" if i % 2 else f"Acme {i} LLC" for i in range(20)
            ],
            "aliases": [[] for _ in range(20)],
            "birth_dates": [["1970-01-01"] for _ in range(20)],
            "countries": [["pk"] for _ in range(20)],
        }
    )
    icij = pl.DataFrame(
        {
            "entity_id": [f"icij:{i}" for i in range(20)],
            "node_type": ["officer" if i % 2 else "entity" for i in range(20)],
            "name": [
                f"Anna Schmidt{chr(97 + i)}" if i % 2 else f"Ocean {i} LIMITED" for i in range(20)
            ],
        }
    )
    return {"accounts": accounts, "opensanctions": opensanctions, "icij": icij}


def _run(inputs: dict[str, pl.DataFrame], config: Settings) -> tuple[pl.DataFrame, pl.DataFrame]:
    customers = generate_customers(inputs["accounts"], config)
    laundering = frozenset({"1:0", "1:1", "1:2", "1:3"})
    return sl.seed_links(
        customers,
        inputs["accounts"],
        laundering,
        inputs["opensanctions"],
        inputs["icij"],
        config,
        "snapshot-x",
    )


def test_seeding_rates_splits_and_ground_truth(seeding_inputs: dict[str, pl.DataFrame]) -> None:
    config = Settings(_env_file=None, seeded_link_rate=0.25, hard_negative_rate=0.1)
    customers, truth = _run(seeding_inputs, config)
    matches = truth.filter(pl.col("is_match"))
    assert matches.height == 10  # 25% of 40
    assert truth.filter(~pl.col("is_match")).height == 4
    assert matches.filter(pl.col("seed_reason") == "laundering_account").height == 4  # all 4
    assert set(matches["source_dataset"]) == {"opensanctions", "icij"}
    assert truth["source_entity_id"].n_unique() == truth.height  # each record seeds once
    assert set(truth["source_snapshot"]) == {"snapshot-x"}

    names = dict(customers.select("customer_id", "name").iter_rows())
    for row in truth.iter_rows(named=True):
        assert names[row["customer_id"]] == row["seeded_name"]
        assert row["seeded_name"] != row["original_name"]


def test_seeded_persons_copy_source_attributes(seeding_inputs: dict[str, pl.DataFrame]) -> None:
    config = Settings(_env_file=None, seeded_link_rate=0.5, seed_opensanctions_share=1.0)
    customers, truth = _run(seeding_inputs, config)
    seeded = truth.filter(pl.col("is_match") & (pl.col("source_dataset") == "opensanctions"))
    joined = seeded.join(customers, on="customer_id")
    assert (joined["country"] == "PK").all()
    persons = joined.filter(pl.col("customer_type") == "person")
    assert persons.height > 0
    assert (persons["date_of_birth"].cast(pl.Utf8) == "1970-01-01").all()


def test_seeding_is_deterministic(seeding_inputs: dict[str, pl.DataFrame]) -> None:
    config = Settings(_env_file=None)
    assert _run(seeding_inputs, config)[1].equals(_run(seeding_inputs, config)[1])
