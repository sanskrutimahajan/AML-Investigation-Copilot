"""Customer ownership and the shared attribute helpers (ADR 006)."""

import numpy as np
import polars as pl

from tracegraph.ingest import customer_attributes as ca
from tracegraph.ingest.synthetic_profiles import attach_customers

ACCOUNTS = pl.DataFrame(
    {
        "account_id": ["1:a", "1:b", "2:c", "3:d"],
        "entity_id": ["E1", "E1", "E2", None],
        "entity_type": ["Individual", "Individual", "Corporation", None],
    }
)


def test_accounts_get_owning_customer() -> None:
    owned = attach_customers(ACCOUNTS)
    assert owned["customer_id"].to_list() == ["cust-E1", "cust-E1", "cust-E2", None]


def test_iso_codes_normalize_to_alpha2() -> None:
    assert ca.iso2("pk") == "PK"
    assert ca.iso2("DEU") == "DE"
    assert ca.iso2("HKG;CHN") == "HK"  # territory missing from Faker's table
    assert ca.iso2("VGB") == "VG"
    assert ca.iso2("suhh") is None
    assert ca.iso2(None) is None


def test_style_name_is_uniform() -> None:
    assert ca.style_name("OCEAN VENTURES TRADING LLC", upper=False) == "Ocean Ventures Trading LLC"
    assert ca.style_name("anna-maria schmidt gmbh", upper=False) == "Anna-Maria Schmidt GmbH"
    assert ca.style_name("Anna Schmidt", upper=True) == "ANNA SCHMIDT"


def test_streets_exclude_buildings_and_numbers() -> None:
    assert ca._street("Vali Asr Avenue 1234, Tehran") == "Vali Asr Avenue"
    assert ca._street("Kumgang Bank Building, Jungsong-dong") is None
    assert ca._street("P.O. Box 1234") is None


def test_only_native_faker_locales_are_used_for_names() -> None:
    locales = ca._native_name_locales()
    assert locales["DE"] == "de_DE"
    assert locales["CN"] == "zh_CN"  # romanized
    assert "JO" not in locales  # ar_JO silently falls back to en_US names
    assert locales.get("CA") != "es_CA"  # Catalan, not Canada
    assert "QC" not in locales
    assert "RU" not in locales  # Cyrillic


def test_latin_share() -> None:
    assert ca.latin_share("José Núñez") == 1.0
    assert ca.latin_share("Иван") == 0.0
    assert np.isclose(ca.latin_share("Ivan Иван"), 0.5)
