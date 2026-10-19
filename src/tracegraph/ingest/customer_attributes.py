"""Customer attributes generated identically for every customer, seeded or not (ADR 006).

Every customer, whether plain, seeded match or look-alike, gets its non-name attributes from a
*template* record drawn from the same OpenSanctions/ICIJ pools by the same rule:
- **Country:** the template's country with probability `customer_source_country_rate` (when an
  address can be generated for it); otherwise one of the base `faker_locales` countries.
- **Date of birth (people):** copied from the template when it has a full date; otherwise Faker.
- **Address:** one format for everyone, built from real city names of the final country.
- **Capitalization:** UPPER with probability `customer_uppercase_rate`, otherwise title case.

For seeded matches and look-alikes the template is their source record; for plain customers it is
a random record. So country, dates, address and casing follow the same distribution whatever the
seed status. Plain names come from the template's name origin: native Faker where Faker really
has that locale, otherwise tokens recombined from same-origin watchlist names. A full watchlist
name is never reproduced.
"""

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

import numpy as np
from faker import Faker
from faker.providers.address import Provider as AddressProvider

from tracegraph.config import Settings

# Faker's ISO table omits territories that dominate offshore data; these complete it.
_ISO3_SUPPLEMENT = {
    "HKG": "HK",
    "VGB": "VG",
    "JEY": "JE",
    "GGY": "GG",
    "IMN": "IM",
    "TWN": "TW",
    "MAC": "MO",
    "CYM": "KY",
    "BMU": "BM",
    "AIA": "AI",
    "TCA": "TC",
    "GIB": "GI",
    "MSR": "MS",
    "ABW": "AW",
    "CUW": "CW",
    "SXM": "SX",
    "PRI": "PR",
    "VIR": "VI",
    "COK": "CK",
    "NIU": "NU",
    "PSE": "PS",
    "XKX": "XK",
    "ANT": "AN",
    "GLP": "GP",
    "MTQ": "MQ",
    "REU": "RE",
    "NCL": "NC",
    "PYF": "PF",
}
ISO3_TO_ISO2: dict[str, str] = {
    **dict(
        zip(
            AddressProvider.alpha_3_country_codes,
            AddressProvider.alpha_2_country_codes,
            strict=True,
        )
    ),
    **_ISO3_SUPPLEMENT,
}
# Locale codes whose region part is not an ISO country (es_CA is Catalan, fr_QC is Quebec).
_NON_COUNTRY_LOCALES = frozenset({"es_CA", "fr_QC"})
# A street value is kept only if it names a street, not a building, bank or company.
_STREET_WORDS = re.compile(
    r"\b(street|st|road|rd|avenue|ave|av|boulevard|blvd|lane|ln|highway|hwy|way|drive|dr|square|"
    r"ulitsa|ul|prospekt|pr|pereulok|shosse|naberezhnaya|khiyaban|khiaban|kheyaban|shari|sharia|"
    r"calle|avenida|carrera|rua|via|viale|strasse|stra[sß]e|jalan|jl|lu|jie|dong|ro|gil|cadde|"
    r"caddesi|sokak|sk|marg|nagar)\b",
    re.IGNORECASE,
)
_NOT_STREET_WORDS = re.compile(
    r"\b(bank|building|bldg|tower|center|centre|company|co|ltd|llc|inc|corporation|plaza|hotel|"
    r"office|floor|room|suite|unit|box|p\.?o)\b",
    re.IGNORECASE,
)
# Same-origin recombination needs enough records to produce names that are not real ones.
MIN_ORIGIN_RECORDS = 30
# Acronym suffixes keep their conventional casing under title case.
_CASED_TOKENS = {
    "llc": "LLC",
    "llp": "LLP",
    "plc": "PLC",
    "jsc": "JSC",
    "ojsc": "OJSC",
    "pjsc": "PJSC",
    "ooo": "OOO",
    "fze": "FZE",
    "fzco": "FZCO",
    "sa": "SA",
    "ag": "AG",
    "bv": "BV",
    "srl": "SRL",
    "gmbh": "GmbH",
    "s.a.": "S.A.",
    "b.v.": "B.V.",
    "l.l.c.": "L.L.C.",
    "s.r.l.": "S.R.L.",
}


@dataclass(frozen=True)
class SourceRecord:
    """A watchlist/offshore record usable as a seed source or an attribute template."""

    entity_id: str
    dataset: str
    name: str
    aliases: tuple[str, ...] = ()
    birth_date: date | None = None
    country: str | None = None  # ISO 3166-1 alpha-2, upper case


def latin_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    kept = sum(bool(unicodedata.normalize("NFKD", c).encode("ascii", "ignore")) for c in letters)
    return kept / len(letters)


def iso2(code: str | None) -> str | None:
    """Normalize ISO2 (OpenSanctions) or ISO3 (ICIJ, ';'-separated) country codes to ISO2."""
    if not code:
        return None
    first = code.split(";")[0].strip().upper()
    if len(first) == 2 and first.isalpha():
        return first
    return ISO3_TO_ISO2.get(first)


def _normalize(name: str) -> str:
    return " ".join(re.sub(r"[^\w ]", " ", name.lower()).split())


def style_name(name: str, upper: bool) -> str:
    """Render a name in one casing style regardless of how its source wrote it."""
    if upper:
        return name.upper()
    tokens = []
    for token in name.split():
        lowered = token.lower()
        if lowered in _CASED_TOKENS:
            tokens.append(_CASED_TOKENS[lowered])
        else:
            tokens.append(
                "-".join(part[:1].upper() + part[1:].lower() for part in token.split("-"))
            )
    return " ".join(tokens)


def _native_name_locales() -> dict[str, str]:
    """ISO2 -> Faker locale whose person names are native and Latin (or romanized).

    Many Faker locales silently fall back to en_US data (e.g. ar_JO, el_CY); a locale counts only
    if its person provider is its own module.
    """
    from faker.config import AVAILABLE_LOCALES

    found: dict[str, str] = {}
    for locale in sorted(AVAILABLE_LOCALES):
        if "_" not in locale or locale in _NON_COUNTRY_LOCALES:
            continue
        fake = Faker(locale)
        person = next(p for p in fake.factories[0].providers if ".person." in type(p).__module__)
        if not type(person).__module__.endswith("." + locale):
            continue
        fake.seed_instance(0)
        romanized = hasattr(fake, "romanized_name")
        sample = [fake.romanized_name() if romanized else fake.name() for _ in range(40)]
        if min(latin_share(name) for name in sample) < 0.8:
            continue
        found.setdefault(locale.split("_")[1], locale)
    return found


def _street(raw: str) -> str | None:
    """The street-name part of an address line: first segment, no numbers, at most 5 words."""
    segment = re.sub(r"[\d#/]+", " ", raw.split(",")[0])
    segment = " ".join(segment.replace("-", " ").split()).strip(" .")
    words = segment.split()
    if not 2 <= len(words) <= 5 or sum(ch.isalpha() for ch in segment) < 4:
        return None
    if not _STREET_WORDS.search(segment) or _NOT_STREET_WORDS.search(segment):
        return None
    return segment


class AttributeFactory:
    """Generates customer attributes and plain names from shared pools; see module docstring."""

    def __init__(
        self,
        pools: dict[tuple[str, str], list[SourceRecord]],
        address_vocabulary: dict[str, dict[str, Counter[str]]],
        config: Settings,
    ) -> None:
        self.pools = pools
        self.config = config
        self.base_countries = sorted({loc.split("_")[1] for loc in config.faker_locales})
        self._fakers = {loc.split("_")[1]: Faker(loc) for loc in config.faker_locales}
        for index, fake in enumerate(self._fakers.values()):
            fake.seed_instance(config.random_seed + index)
        self._name_locales = _native_name_locales()
        self._name_fakers: dict[str, Faker] = {}

        self.cities: dict[str, tuple[list[str], np.ndarray]] = {}
        self.streets: dict[str, list[str]] = {}
        for country, vocab in address_vocabulary.items():
            cities = [c for c in vocab.get("cities", Counter()) if latin_share(c) >= 0.95]
            if cities:
                weights = np.array([vocab["cities"][c] for c in cities], dtype=float)
                self.cities[country] = (cities, weights / weights.sum())
            streets = sorted(
                {
                    street
                    for raw in vocab.get("streets", Counter())
                    if latin_share(raw) >= 0.95 and (street := _street(raw))
                }
            )
            if streets:
                self.streets[country] = streets
        for country in self.base_countries:
            self.cities.pop(country, None)  # base countries use native Faker cities
        self.addressable = set(self.cities) | set(self.base_countries)

        self.all_names = {_normalize(r.name) for pool in pools.values() for r in pool}
        self._person_tokens: dict[str | None, tuple[list[list[str]], list[str]]] = {}
        self._org_tokens: dict[str | None, tuple[list[str], list[str], list[int]]] = {}
        self._build_vocabularies()

    # --- vocabularies ------------------------------------------------------------------------

    def _build_vocabularies(self) -> None:
        from tracegraph.ingest.seed_links import suffix_removal  # shared suffix rules

        given: dict[str | None, list[list[str]]] = defaultdict(list)
        surnames: dict[str | None, list[str]] = defaultdict(list)
        org_core: dict[str | None, list[str]] = defaultdict(list)
        org_suffix: dict[str | None, list[str]] = defaultdict(list)
        org_lengths: dict[str | None, list[int]] = defaultdict(list)
        for (_, kind), pool in self.pools.items():
            for record in pool:
                # Tokens without a letter ("-", "&", "2") would make names like "-".
                tokens = [t for t in record.name.split() if any(c.isalpha() for c in t)]
                if not tokens:
                    continue
                for origin in (record.country, "*"):
                    if kind == "person":
                        given[origin].append(tokens[:-1])
                        surnames[origin].append(tokens[-1])
                    else:
                        stripped = suffix_removal(record.name, np.random.default_rng(0))
                        core = [
                            t
                            for t in (stripped or record.name).split()
                            if any(c.isalpha() for c in t)
                        ] or tokens
                        suffix = record.name[len(stripped) :].strip(" ,") if stripped else ""
                        org_core[origin].extend(core)
                        org_suffix[origin].append(suffix)
                        org_lengths[origin].append(len(core))
        for origin in given:
            self._person_tokens[origin] = (given[origin], surnames[origin])
        for origin in org_core:
            self._org_tokens[origin] = (org_core[origin], org_suffix[origin], org_lengths[origin])

    # --- templates and attributes --------------------------------------------------------------

    def template(self, customer_type: str, rng: np.random.Generator) -> SourceRecord:
        """A random pool record, drawn by the same source mix as seeds."""
        preferred = (
            "opensanctions" if rng.random() < self.config.seed_opensanctions_share else "icij"
        )
        for dataset in (preferred, "icij" if preferred == "opensanctions" else "opensanctions"):
            pool = self.pools.get((dataset, customer_type), [])
            if pool:
                return pool[int(rng.integers(len(pool)))]
        raise ValueError(f"no template records for customer type {customer_type!r}")

    def country(self, template: SourceRecord, rng: np.random.Generator) -> tuple[str, bool]:
        """(country, copied_from_template)."""
        if (
            template.country in self.addressable
            and rng.random() < self.config.customer_source_country_rate
        ):
            return str(template.country), True
        return str(rng.choice(self.base_countries)), False

    def address(self, country: str, rng: np.random.Generator) -> str:
        number = int(rng.integers(1, 300))
        if country in self._fakers:
            fake = self._fakers[country]
            return f"{number} {fake.street_name()}, {fake.city()}"
        cities, weights = self.cities[country]
        city = cities[int(rng.choice(len(cities), p=weights))]
        streets = self.streets.get(country)
        if streets:
            street = streets[int(rng.integers(len(streets)))]
        else:
            street = self._fakers[self.base_countries[0]].street_name()
        return f"{number} {street}, {city}"

    def date_of_birth(self, template: SourceRecord) -> date | None:
        return template.birth_date

    def faker_date_of_birth(self) -> date:
        return self._fakers[self.base_countries[0]].date_of_birth(minimum_age=18, maximum_age=90)

    def incorporation_date(self) -> date:
        fake = self._fakers[self.base_countries[0]]
        return fake.date_between(date(1950, 1, 1), date(2021, 12, 31))

    # --- plain names ---------------------------------------------------------------------------

    def _name_faker(self, locale: str) -> Faker:
        if locale not in self._name_fakers:
            fake = Faker(locale)
            fake.seed_instance(self.config.random_seed + len(self._name_fakers) + 100)
            self._name_fakers[locale] = fake
        return self._name_fakers[locale]

    def _fresh(self, name: str) -> bool:
        return _normalize(name) not in self.all_names

    def person_name(self, origin: str | None, rng: np.random.Generator) -> str:
        """A person name from the template's origin that is not any watchlist name."""
        given_pool = self._person_tokens.get(origin)
        if given_pool is None or len(given_pool[1]) < MIN_ORIGIN_RECORDS:
            given_pool = self._person_tokens["*"]  # too few same-origin names to recombine safely
        for attempt in range(100):
            if attempt == 50:
                given_pool = self._person_tokens["*"]
            if origin in self._name_locales:
                fake = self._name_faker(self._name_locales[str(origin)])
                name = (
                    fake.romanized_name()
                    if hasattr(fake, "romanized_name")
                    else f"{fake.first_name()} {fake.last_name()}"
                )
            else:
                given, surnames = given_pool
                first = given[int(rng.integers(len(given)))] or [str(rng.choice(surnames))]
                name = " ".join([*first, surnames[int(rng.integers(len(surnames)))]])
            if self._fresh(name) and len(name.split()) >= 2:
                return name
        raise ValueError(f"could not generate a fresh person name for origin {origin!r}")

    def organization_name(self, origin: str | None, rng: np.random.Generator) -> str:
        """Watchlist-vocabulary tokens recombined; never a full watchlist name."""
        core, suffixes, lengths = self._org_tokens.get(origin) or self._org_tokens["*"]
        if len(lengths) < MIN_ORIGIN_RECORDS:
            core, suffixes, lengths = self._org_tokens["*"]
        for attempt in range(100):
            if attempt == 50:
                core, suffixes, lengths = self._org_tokens["*"]
            n = max(1, lengths[int(rng.integers(len(lengths)))])
            tokens: list[str] = []
            while len(tokens) < n:
                token = core[int(rng.integers(len(core)))]
                if token.lower() not in {t.lower() for t in tokens}:
                    tokens.append(token)
            suffix = suffixes[int(rng.integers(len(suffixes)))]
            name = " ".join([*tokens, suffix]).strip()
            if self._fresh(name):
                return name
        raise ValueError(f"could not generate a fresh organization name for origin {origin!r}")

    def lookalike(self, record: SourceRecord, *, person: bool, rng: np.random.Generator) -> str:
        """Similar-looking but different identity: shared surname / lead token, rest generated."""
        tokens = record.name.split()
        for _ in range(50):
            if person:
                other = self.person_name(record.country, rng).split()
                name = " ".join([*other[:-1], tokens[-1]])
            else:
                other = self.organization_name(record.country, rng).split()
                name = (
                    " ".join([tokens[0], *other[1:]])
                    if len(other) > 1
                    else f"{tokens[0]} {other[0]}"
                )
            if self._fresh(name) and _normalize(name) != _normalize(record.name):
                return name
        raise ValueError(f"could not generate a look-alike for {record.entity_id}")
