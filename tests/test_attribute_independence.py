"""Every non-name attribute is distributed the same for every role (ADR 007).

Each role (seed, look-alike, homonym) is compared with plain customers on: country, DOB presence,
DOB year, incorporation year, address length, casing, "Last, First" format, and edit count. A
chi-square (categorical) or Kolmogorov-Smirnov (numeric) test must not reject at p < 0.001.
"""

from typing import Any

import numpy as np
import polars as pl
import pytest
import test_seed_links
from scipy import stats

from tracegraph.config import Settings

ALPHA = 0.001
ROLES = ("seeded_match", "hard_negative", "homonym")


@pytest.fixture(scope="module")
def customers() -> pl.DataFrame:
    config = Settings(
        _env_file=None, seeded_link_rate=0.2, hard_negative_rate=0.15, homonym_rate=0.15
    )
    inputs = test_seed_links.seeding_inputs.__wrapped__()
    build = test_seed_links._build(inputs, config)
    return (
        build.customers.join(
            build.ground_truth.select("customer_id", "link_type"), on="customer_id", how="left"
        )
        .join(build.name_edits.select("customer_id", "edits_drawn"), on="customer_id")
        .with_columns(
            pl.col("link_type").fill_null("plain"),
            pl.col("date_of_birth").is_not_null().alias("has_dob"),
            pl.col("date_of_birth").dt.year().alias("dob_year"),
            pl.col("incorporation_date").dt.year().alias("inc_year"),
            pl.col("address").str.len_chars().alias("address_length"),
            (pl.col("name") == pl.col("name").str.to_uppercase()).alias("upper"),
            pl.col("name").str.contains(",", literal=True).alias("comma"),
        )
    )


def _chi_square(a: pl.Series, b: pl.Series) -> float:
    values = sorted(set(a.drop_nulls().to_list()) | set(b.drop_nulls().to_list()), key=str)
    table = np.array([[int((x == v).sum()) for v in values] for x in (a, b)])
    table = table[:, table.sum(axis=0) > 0]
    if table.shape[1] < 2:
        return 1.0
    return float(stats.chi2_contingency(table)[1])


def _ks(a: pl.Series, b: pl.Series) -> float:
    a, b = a.drop_nulls(), b.drop_nulls()
    if a.len() < 5 or b.len() < 5:
        return 1.0
    return float(stats.ks_2samp(a.to_numpy(), b.to_numpy()).pvalue)


CHECKS: dict[str, tuple[Any, str | None]] = {
    "country": (_chi_square, None),
    "has_dob": (_chi_square, "person"),
    "dob_year": (_ks, "person"),
    "inc_year": (_ks, "organization"),
    "address_length": (_ks, None),
    "upper": (_chi_square, None),
    "comma": (_chi_square, "person"),
    "edits_drawn": (_chi_square, None),
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("attribute", list(CHECKS))
def test_attribute_distribution_does_not_depend_on_role(
    customers: pl.DataFrame, role: str, attribute: str
) -> None:
    test, kind = CHECKS[attribute]
    frame = customers if kind is None else customers.filter(pl.col("customer_type") == kind)
    plain = frame.filter(pl.col("link_type") == "plain")[attribute]
    other = frame.filter(pl.col("link_type") == role)[attribute]
    assert other.len() > 10, f"too few {role} customers to test"
    p_value = test(plain, other)
    assert p_value >= ALPHA, f"{attribute} differs for {role} vs plain (p={p_value:.2g})"
