"""ER normalization and blocking keys."""

import polars as pl

from tracegraph.er import normalize as nz
from tracegraph.er.blocking import add_blocking_keys, candidate_pairs


def test_normalize_folds_scripts_and_punctuation() -> None:
    assert nz.normalize_name("José  Núñez-García") == "jose nunez garcia"
    assert nz.normalize_name("Сергей Петров") == "sergey petrov"
    assert nz.normalize_name("OCEAN VENTURES, LLC.") == "ocean ventures llc"


def test_legal_suffix_classes() -> None:
    assert nz.split_legal_suffix("ocean ventures llc") == ("ocean ventures", "llc")
    assert nz.split_legal_suffix("hotfocus co ltd") == ("hotfocus", "ltd")
    assert nz.split_legal_suffix("alpha limited") == ("alpha", "ltd")
    assert nz.split_legal_suffix("ooo romashka") == ("ooo romashka", None)  # prefix, not suffix
    assert nz.split_legal_suffix("limited") == ("limited", None)  # core keeps a token


def test_tokens_dates_and_phonetics() -> None:
    assert nz.name_tokens("petrov sergei") == nz.name_tokens("sergei petrov")
    assert nz.birth_year("1965") == 1965
    assert nz.parse_date("1965") is None
    assert nz.parse_date("1970-02-03").isoformat() == "1970-02-03"  # type: ignore[union-attr]
    assert nz.phonetic_key("mohammed") == nz.phonetic_key("mohamed")


def _records(names: list[str], prefix: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "unique_id": [f"{prefix}{i}" for i in range(len(names))],
            "kind": ["person"] * len(names),
            "tokens": [sorted(set(n.split())) for n in names],
        }
    )


def test_rare_token_blocking_skips_common_tokens() -> None:
    customers = _records(["anna zelinsky", "omar trading"], "c")
    sources = _records(
        ["anna zelinskiy", "global trading"] + [f"x{i} trading" for i in range(5)], "s"
    )
    c, s = add_blocking_keys(customers, sources, max_frequency=3)
    assert "trading" not in c["rare_tokens"][1]  # 7 records share it
    pairs = candidate_pairs(c, s)
    assert ("c0", "s0") in set(zip(pairs["customer_id"], pairs["entity_id"], strict=True))
    found = pairs.filter((pl.col("customer_id") == "c0") & (pl.col("entity_id") == "s0"))
    assert "rare_tokens" in found["found_by"][0]  # shared rare token "anna"
