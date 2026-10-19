"""Name perturbations and seeded-link ground truth."""

from collections import Counter
from typing import Any

import numpy as np
import polars as pl
import pytest

from tracegraph.config import Settings
from tracegraph.ingest import seed_links as sl
from tracegraph.ingest.staging_schemas import validate
from tracegraph.ingest.synthetic_profiles import attach_customers


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


def test_typo_handles_hyphenated_single_tokens() -> None:
    for seed in range(10):
        result = sl.typo("KUDO-KAI", np.random.default_rng(seed))
        assert result is not None and _edit_distance_one(result, "KUDO-KAI")


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


def test_perturb_applies_exactly_the_requested_number_of_edits() -> None:
    for n_edits in (0, 1, 2):
        for seed in range(20):
            name, applied = sl.perturb(
                "Ocean Ventures Trading LLC",
                person=False,
                n_edits=n_edits,
                rng=np.random.default_rng(seed),
            )
            assert len(applied) == n_edits
            assert set(applied) <= set(sl.EDITS)
            assert (name == "Ocean Ventures Trading LLC") == (n_edits == 0)


def test_person_format_conventions() -> None:
    assert sl.canonical_person_order("Petrov, Sergei Ivanovich") == "Sergei Ivanovich Petrov"
    assert sl.canonical_person_order("Sergei Petrov") == "Sergei Petrov"
    assert sl.comma_format("Sergei Ivanovich Petrov") == "Petrov, Sergei Ivanovich"
    assert "," not in (sl.token_reorder("Sergei Petrov", rng()) or "")


def test_edit_count_distribution() -> None:
    draws = [sl.draw_edit_count([0.3, 0.45, 0.25], np.random.default_rng(i)) for i in range(2000)]
    counts = np.bincount(draws, minlength=3) / len(draws)
    assert np.allclose(counts, [0.3, 0.45, 0.25], atol=0.04)


@pytest.fixture
def seeding_inputs() -> dict[str, Any]:
    n = 600
    accounts = attach_customers(
        pl.DataFrame(
            {
                "account_id": [f"1:{i}" for i in range(n)],
                "entity_id": [f"E{i:02d}" for i in range(n)],
                "entity_type": ["Individual" if i % 2 else "Corporation" for i in range(n)],
            }
        )
    )
    given = ["Ahmad", "Sergei", "Omar", "Ivan", "Yusuf", "Karim", "Dmitri", "Hassan"]
    surnames = ["Khan", "Petrov", "Haddad", "Volkov", "Rahimi", "Saleh", "Orlov", "Nasser"]
    words = ["Ocean", "Global", "Trade", "Star", "Delta", "Union", "Atlas", "Nova", "Prime"]
    m = 800
    opensanctions = pl.DataFrame(
        {
            "entity_id": [f"opensanctions:{i}" for i in range(m)],
            "schema": ["Person" if i % 2 else "Organization" for i in range(m)],
            "name": [
                f"{given[i % 8]} {given[(i // 8) % 8]} {surnames[(i * 3) % 8]}"
                if i % 2
                else f"{words[i % 9]} {words[(i // 9) % 9]} {i} LLC"
                for i in range(m)
            ],
            "aliases": [[] for _ in range(m)],
            "birth_dates": [["1970-01-01"] if i % 4 == 1 else ["1965"] for i in range(m)],
            "countries": [["pk"] if i % 3 else ["ae"] for i in range(m)],
        }
    )
    icij = pl.DataFrame(
        {
            "entity_id": [f"icij:{i}" for i in range(m)],
            "node_type": ["officer" if i % 2 else "entity" for i in range(m)],
            "name": [
                f"{given[(i + 3) % 8]} {surnames[i % 8]}"
                if i % 2
                else f"{words[(i + 4) % 9]} {words[i % 9]} {i} LIMITED"
                for i in range(m)
            ],
            "country_codes": ["PAK" if i % 2 else "ARE" for i in range(m)],
        }
    )
    vocabulary = {
        "PK": {
            "cities": Counter({"Karachi": 3, "Lahore": 1}),
            "streets": Counter({"Mall Road": 1}),
        },
        "AE": {"cities": Counter({"Dubai": 2}), "streets": Counter({"Sheikh Zayed Road": 1})},
    }
    return {
        "accounts": accounts,
        "opensanctions": opensanctions,
        "icij": icij,
        "vocabulary": vocabulary,
    }


def _run(inputs: dict[str, Any], config: Settings) -> tuple[pl.DataFrame, pl.DataFrame]:
    build = sl.build_customers(
        inputs["accounts"],
        frozenset({"1:0", "1:1", "1:2", "1:3"}),
        inputs["opensanctions"],
        inputs["icij"],
        inputs["vocabulary"],
        config,
        "snapshot-x",
    )
    return build.customers, build.ground_truth


def _build(inputs: dict[str, Any], config: Settings) -> sl.CustomerBuild:
    return sl.build_customers(
        inputs["accounts"],
        frozenset({"1:0", "1:1", "1:2", "1:3"}),
        inputs["opensanctions"],
        inputs["icij"],
        inputs["vocabulary"],
        config,
        "snapshot-x",
    )


def test_seeding_rates_splits_and_ground_truth(seeding_inputs: dict[str, Any]) -> None:
    config = Settings(_env_file=None, seeded_link_rate=0.25, hard_negative_rate=0.1)
    customers, truth = _run(seeding_inputs, config)
    validate("customers", customers)
    assert customers.height == 600
    matches = truth.filter(pl.col("is_match"))
    assert matches.height == 150  # 25% of 600
    assert truth.filter(~pl.col("is_match")).height == 60
    assert matches.filter(pl.col("seed_reason") == "laundering_account").height == 4  # all 4
    assert set(matches["source_dataset"]) == {"opensanctions", "icij"}
    assert truth["source_entity_id"].n_unique() == truth.height  # each record seeds once
    assert set(truth["source_snapshot"]) == {"snapshot-x"}

    names = dict(customers.select("customer_id", "name").iter_rows())
    for row in truth.iter_rows(named=True):
        assert names[row["customer_id"]] == row["seeded_name"]


def test_edits_are_role_independent(seeding_inputs: dict[str, Any]) -> None:
    config = Settings(_env_file=None, seeded_link_rate=0.3, hard_negative_rate=0.2)
    build = _build(seeding_inputs, config)
    roles = build.name_edits.join(
        build.ground_truth.select("customer_id", "link_type"), on="customer_id", how="left"
    ).with_columns(pl.col("link_type").fill_null("plain"))
    by_role = {role: frame for (role,), frame in roles.group_by("link_type")}
    assert set(by_role) == {"plain", "seeded_match", "hard_negative"}

    def distribution(frame: pl.DataFrame, column: str) -> np.ndarray:
        values = frame[column].to_numpy() if column == "edits_drawn" else frame[column].list.len()
        return np.bincount(np.asarray(values), minlength=3)[:3] / frame.height

    plain = by_role["plain"]
    for role in ("seeded_match", "hard_negative"):
        other = by_role[role]
        for column in ("edits_drawn", "edits"):  # drawn and realized edit counts
            gap = np.abs(distribution(plain, column) - distribution(other, column)).sum() / 2
            assert gap < 0.12, (role, column, gap)
        rates = {f"edit:{e}": plain["edits"].list.contains(e) for e in sl.EDITS}
        others = {f"edit:{e}": other["edits"].list.contains(e) for e in sl.EDITS}
        rates["upper"], others["upper"] = plain["upper"], other["upper"]
        for key in rates:  # per-edit and casing rates: within 4 standard errors of each other
            a, b = float(rates[key].mean()), float(others[key].mean())
            pooled = (a * plain.height + b * other.height) / (plain.height + other.height)
            se = np.sqrt(max(pooled * (1 - pooled), 1e-9) * (1 / plain.height + 1 / other.height))
            assert abs(a - b) <= 4 * se, (role, key, a, b)
    assert (roles["edits_drawn"] == 0).any()  # zero edits are possible for every role
    assert (by_role["seeded_match"]["edits_drawn"] == 0).any()


def test_plain_names_never_reproduce_a_source_name(seeding_inputs: dict[str, Any]) -> None:
    customers, truth = _run(seeding_inputs, Settings(_env_file=None))
    plain = customers.filter(~pl.col("customer_id").is_in(truth["customer_id"].implode()))
    sources = {
        n.lower()
        for n in seeding_inputs["opensanctions"]["name"].to_list()
        + seeding_inputs["icij"]["name"].to_list()
    }
    assert plain.height > 0
    assert not {n.lower() for n in plain["name"]} & sources


def test_attributes_follow_one_rule_for_every_role(seeding_inputs: dict[str, Any]) -> None:
    config = Settings(_env_file=None, seeded_link_rate=0.3, hard_negative_rate=0.1)
    customers, truth = _run(seeding_inputs, config)
    roles = customers.join(truth.select("customer_id", "link_type"), on="customer_id", how="left")
    plain = roles.filter(pl.col("link_type").is_null())
    base = {loc.split("_")[1] for loc in config.faker_locales}
    # Plain customers also take watchlist countries from templates, not only base countries.
    assert set(plain["country"]) - base
    assert set(roles["country"]) <= base | {"PK", "AE"}
    # Watchlist dates of birth appear for plain customers too (from templates).
    assert (plain["date_of_birth"].cast(pl.Utf8) == "1970-01-01").any()
    # Casing is uniform: every name is either fully upper case or title case.
    for name in customers["name"]:
        assert name == name.upper() or name == sl.style_name(name, upper=False)


def test_copied_attributes_are_recorded(seeding_inputs: dict[str, Any]) -> None:
    customers, truth = _run(seeding_inputs, Settings(_env_file=None, seeded_link_rate=0.5))
    joined = truth.join(customers, on="customer_id")
    for row in joined.iter_rows(named=True):
        if "country" in row["copied_attributes"]:
            assert row["country"] in {"PK", "AE"}
        if "date_of_birth" in row["copied_attributes"]:
            assert str(row["date_of_birth"]) == "1970-01-01"


def test_seeding_is_deterministic(seeding_inputs: dict[str, Any]) -> None:
    config = Settings(_env_file=None)
    first, second = _run(seeding_inputs, config), _run(seeding_inputs, config)
    assert first[0].equals(second[0])
    assert first[1].equals(second[1])
