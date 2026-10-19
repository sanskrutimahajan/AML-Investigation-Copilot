"""Build all customers; seed some with perturbed OpenSanctions/ICIJ names (ER ground truth).

Every seeded link is synthetic: it says nothing about any real person or company. The answer key is
returned separately and written only to `seed_ground_truth.parquet`; no gold `SAME_AS` edge is made.
Plain, seeded and look-alike customers share one attribute procedure (ADR 006), so nothing apart
from the name relationship to a source record depends on seed status.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from tracegraph.config import Settings
from tracegraph.ingest.customer_attributes import (
    AttributeFactory,
    SourceRecord,
    iso2,
    latin_share,
    style_name,
)
from tracegraph.ingest.synthetic_profiles import CUSTOMER_SCHEMA, PERSON_ENTITY_TYPES, customer_id

PERTURBATIONS = (
    "alias",
    "lookalike",
    "typo",
    "transliteration",
    "token_reorder",
    "suffix_removal",
    "initials",
)
# Common romanization variants; matched case-insensitively on whole tokens.
TRANSLITERATION_VARIANTS: dict[str, tuple[str, ...]] = {
    "mohammed": ("muhammad", "mohamed", "mohammad"),
    "muhammad": ("mohammed", "mohamed", "muhammed"),
    "mohammad": ("muhammad", "mohamad"),
    "ahmed": ("ahmad", "ahmet"),
    "ahmad": ("ahmed",),
    "yusuf": ("yousef", "youssef"),
    "hussein": ("husain", "hussain", "husayn"),
    "abdul": ("abdel", "abd al"),
    "abdel": ("abdul",),
    "aleksandr": ("alexander", "aleksander"),
    "alexander": ("aleksandr",),
    "sergei": ("sergey",),
    "sergey": ("sergei",),
    "yuri": ("yury", "iurii"),
    "dmitry": ("dmitri", "dmitriy"),
    "andrei": ("andrey",),
    "andrey": ("andrei",),
    "nikolai": ("nikolay",),
    "alexei": ("alexey", "aleksei"),
    "evgeny": ("yevgeny", "evgeniy"),
    "mikhail": ("michail",),
    "qasim": ("kassem", "qassem"),
    "osama": ("usama",),
    "jose": ("josé",),
}
_RULE_SUBSTITUTIONS = (("kh", "h"), ("ph", "f"), ("ou", "u"), ("ee", "i"), ("y", "i"), ("w", "v"))
ORG_SUFFIXES = (
    "s.a. de c.v.",
    "co., ltd.",
    "co. ltd",
    "co., ltd",
    "limited",
    "ltd.",
    "ltd",
    "llc",
    "l.l.c.",
    "inc.",
    "inc",
    "corp.",
    "corporation",
    "company",
    "co.",
    "gmbh",
    "s.a.",
    "sa",
    "jsc",
    "ojsc",
    "pjsc",
    "ooo",
    "llp",
    "plc",
    "fze",
    "fzco",
    "ag",
    "bv",
    "b.v.",
    "srl",
    "s.r.l.",
    "pte. ltd.",
    "pte ltd",
)
_USABLE_NAME = re.compile(r"[A-Za-z0-9 .,'&()/-]{3,80}")
# ICIJ officer placeholders for bearer shares etc. are not identities and must never seed a link.
PLACEHOLDER_NAMES = frozenset(
    {"the bearer", "bearer", "el portador", "al portador", "portador", "bearer shares"}
)
# ICIJ "officers" are often companies (nominees, trustees); route these to the organization pool.
_ORG_MARKERS = re.compile(
    r"\b(limited|ltd|inc|corp|corporation|company|s\.a|llc|nominees?|trust|trustees?|services|"
    r"holdings?|foundation|establishment|gmbh|bank|group|management|secretaries)\b",
    re.IGNORECASE,
)

GROUND_TRUTH_SCHEMA: dict[str, pl.DataType] = {
    "customer_id": pl.Utf8(),
    "link_type": pl.Utf8(),
    "is_match": pl.Boolean(),
    "source_entity_id": pl.Utf8(),
    "source_dataset": pl.Utf8(),
    "original_name": pl.Utf8(),
    "seeded_name": pl.Utf8(),
    "perturbations": pl.List(pl.Utf8),
    "seed_reason": pl.Utf8(),
    "copied_attributes": pl.List(pl.Utf8),
    "random_seed": pl.Int64(),
    "source_snapshot": pl.Utf8(),
}


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def is_placeholder(name: str) -> bool:
    normalized = " ".join(re.sub(r"[^a-z ]", " ", name.lower()).split())
    if normalized in PLACEHOLDER_NAMES:
        return True
    # "THE BEARER (AGENT ...)" style variants; bare "bearer" alone is too common to prefix-match.
    return any(normalized.startswith(p + " ") for p in PLACEHOLDER_NAMES if " " in p)


def looks_like_organization(name: str) -> bool:
    return bool(_ORG_MARKERS.search(name))


def is_usable_name(name: str, *, person: bool) -> bool:
    """Latin-script names with enough letters (and two tokens for people) make realistic seeds."""
    if not name or is_placeholder(name) or latin_share(name) < 0.9:
        return False
    if not _USABLE_NAME.fullmatch(ascii_fold(name)):
        return False
    if sum(ch.isalpha() for ch in name) < 3:
        return False
    return len(name.split()) >= 2 if person else True


# --- perturbations: each returns the new name, or None when it does not apply -------------------


def typo(name: str, rng: np.random.Generator) -> str | None:
    tokens = name.split()

    def positions(token: str) -> list[int]:
        # Interior positions whose character and right neighbour are letters (hyphens allowed).
        return [
            p for p in range(1, len(token) - 1) if token[p].isalpha() and token[p + 1].isalpha()
        ]

    candidates = [
        i for i, t in enumerate(tokens) if sum(c.isalpha() for c in t) >= 4 and positions(t)
    ]
    if not candidates:
        return None
    i = int(rng.choice(candidates))
    token = tokens[i]
    pos = int(rng.choice(positions(token)))
    match int(rng.integers(4)):
        case 0:
            token = token[:pos] + token[pos + 1] + token[pos] + token[pos + 2 :]
        case 1:
            token = token[:pos] + token[pos + 1 :]
        case 2:
            token = token[:pos] + token[pos] + token[pos:]
        case _:
            letter = chr(int(rng.integers(26)) + ord("a"))
            letter = letter.upper() if token[pos].isupper() else letter
            token = token[:pos] + letter + token[pos + 1 :]
    tokens[i] = token
    result = " ".join(tokens)
    return result if result != name else None


def _match_case(template: str, word: str) -> str:
    if template.isupper():
        return word.upper()
    return word.capitalize() if template[:1].isupper() else word


def transliteration(name: str, rng: np.random.Generator) -> str | None:
    tokens = name.split()
    known = [i for i, t in enumerate(tokens) if t.lower() in TRANSLITERATION_VARIANTS]
    if known:
        i = int(rng.choice(known))
        variant = str(rng.choice(TRANSLITERATION_VARIANTS[tokens[i].lower()]))
        tokens[i] = _match_case(tokens[i], variant)
        return " ".join(tokens)
    folded = ascii_fold(name)
    if folded != name:
        return folded
    for old, new in _RULE_SUBSTITUTIONS:
        found = re.search(old, name, re.IGNORECASE)
        if found:
            start, end = found.span()
            return name[:start] + _match_case(found.group(0), new) + name[end:]
    return None


def token_reorder(name: str, rng: np.random.Generator) -> str | None:
    """Surname first, without a comma (the "Last, First" format is applied separately to all)."""
    tokens = name.split()
    if len(tokens) < 2:
        return None
    return " ".join([tokens[-1], *tokens[:-1]])


def canonical_person_order(name: str) -> str:
    """Undo a source's "Last, First" convention so every role starts from given-name order."""
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        if last and first:
            return f"{first} {last}"
    return name


def comma_format(name: str) -> str:
    """Render a person name in "Last, First" format."""
    tokens = name.split()
    return f"{tokens[-1]}, {' '.join(tokens[:-1])}" if len(tokens) >= 2 else name


def suffix_removal(name: str, rng: np.random.Generator) -> str | None:
    lowered = name.lower().rstrip()
    for suffix in sorted(ORG_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(" " + suffix) or lowered.endswith("," + suffix):
            stripped = name[: len(lowered) - len(suffix)].rstrip(" ,")
            return stripped if len(stripped) >= 3 else None
    return None


def initials(name: str, rng: np.random.Generator) -> str | None:
    tokens = name.split()
    if len(tokens) < 2 or not tokens[0][:1].isalpha():
        return None
    return " ".join([f"{tokens[0][0].upper()}.", *tokens[1:]])


_OPERATIONS: dict[str, Callable[[str, np.random.Generator], str | None]] = {
    "typo": typo,
    "transliteration": transliteration,
    "token_reorder": token_reorder,
    "suffix_removal": suffix_removal,
    "initials": initials,
}


EDITS = ("typo", "transliteration", "token_reorder", "suffix_removal", "initials")


def draw_edit_count(weights: list[float], rng: np.random.Generator) -> int:
    """Number of data-entry edits; one distribution (including 0) for every role."""
    p = np.asarray(weights, dtype=float)
    return int(rng.choice(len(p), p=p / p.sum()))


def perturb(
    name: str, *, person: bool, n_edits: int, rng: np.random.Generator
) -> tuple[str, list[str]]:
    """Apply `n_edits` data-entry edits to any customer's name; returns (name, edits applied).

    The same function and the same edit-count distribution are used for plain, look-alike and
    seeded customers (ADR 006), so edit footprints say nothing about seed status. Edits that do
    not change this particular name are skipped in favour of the next applicable one.
    """
    candidates = ["typo", "transliteration"]
    candidates += ["token_reorder", "initials"] if person else ["suffix_removal"]
    order = [str(op) for op in rng.permutation(candidates)]
    applied: list[str] = []
    for op in order:
        if len(applied) == n_edits:
            break
        result = _OPERATIONS[op](name, rng)
        if result is not None and result != name:
            name, applied = result, [*applied, op]
    return name, applied


# --- source pools ---------------------------------------------------------------------------------


def _parse_birth_date(values: list[str]) -> date | None:
    for value in values:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
    return None


def build_pools(
    opensanctions: pl.DataFrame, icij: pl.DataFrame
) -> dict[tuple[str, str], list[SourceRecord]]:
    """Usable seed/template records keyed by (source dataset, customer type)."""
    pools: dict[tuple[str, str], list[SourceRecord]] = {}
    for row in opensanctions.sort("entity_id").iter_rows(named=True):
        person = row["schema"] == "Person"
        if not is_usable_name(row["name"], person=person):
            continue
        record = SourceRecord(
            entity_id=row["entity_id"],
            dataset="opensanctions",
            name=row["name"],
            aliases=tuple(row["aliases"]),
            birth_date=_parse_birth_date(row["birth_dates"]) if person else None,
            country=next((c for c in map(iso2, row["countries"]) if c), None),
        )
        pools.setdefault(("opensanctions", "person" if person else "organization"), []).append(
            record
        )
    for row in (
        icij.filter(pl.col("node_type").is_in(["officer", "entity"]))
        .sort("entity_id")
        .iter_rows(named=True)
    ):
        if not row["name"]:
            continue
        person = row["node_type"] == "officer" and not looks_like_organization(row["name"])
        if is_usable_name(row["name"], person=person):
            pools.setdefault(("icij", "person" if person else "organization"), []).append(
                SourceRecord(
                    entity_id=row["entity_id"],
                    dataset="icij",
                    name=row["name"],
                    country=iso2(row["country_codes"]),
                )
            )
    return pools


def _draw(
    pools: dict[tuple[str, str], list[SourceRecord]],
    preferred: str,
    customer_type: str,
    used: set[str],
    rng: np.random.Generator,
) -> SourceRecord:
    for dataset in (preferred, "icij" if preferred == "opensanctions" else "opensanctions"):
        pool = [r for r in pools.get((dataset, customer_type), []) if r.entity_id not in used]
        if pool:
            record = pool[int(rng.integers(len(pool)))]
            used.add(record.entity_id)
            return record
    raise ValueError(f"no unused seed records left for customer type {customer_type!r}")


def _sample(ids: list[str], n: int, rng: np.random.Generator) -> list[str]:
    n = min(n, len(ids))
    return sorted(ids[i] for i in rng.choice(len(ids), size=n, replace=False)) if n else []


# --- main entry point -----------------------------------------------------------------------------


NAME_EDITS_SCHEMA: dict[str, pl.DataType] = {
    "customer_id": pl.Utf8(),
    "edits_drawn": pl.Int64(),
    "edits": pl.List(pl.Utf8),
    "upper": pl.Boolean(),
}


@dataclass(frozen=True)
class CustomerBuild:
    customers: pl.DataFrame
    ground_truth: pl.DataFrame
    # Eval-only: data-entry edits drawn and applied for every customer (role-independence checks).
    name_edits: pl.DataFrame


def build_customers(
    accounts: pl.DataFrame,
    laundering_account_ids: frozenset[str],
    opensanctions: pl.DataFrame,
    icij: pl.DataFrame,
    address_vocabulary: dict[str, dict[str, Counter[str]]],
    config: Settings,
    source_snapshot: str,
) -> CustomerBuild:
    """One customer per IBM entity, plus seed ground truth and the eval-only edit log."""
    entities = (
        accounts.filter(pl.col("entity_id").is_not_null())
        .select("entity_id", "entity_type")
        .unique("entity_id", keep="first")
        .sort("entity_id")
    )
    pools = build_pools(opensanctions, icij)
    factory = AttributeFactory(pools, address_vocabulary, config)
    role_rng = np.random.default_rng(config.random_seed + 1)
    attribute_rng = np.random.default_rng(config.random_seed + 2)
    noise_rng = np.random.default_rng(config.random_seed + 3)  # same draws whatever the role

    laundering_customers = set(
        accounts.filter(pl.col("account_id").is_in(list(laundering_account_ids)))
        .get_column("entity_id")
        .drop_nulls()
        .map_elements(customer_id, return_dtype=pl.Utf8)
        .to_list()
    )
    all_ids = sorted(customer_id(e) for e in entities["entity_id"])
    n_seed = round(config.seeded_link_rate * len(all_ids))
    from_laundering = _sample(
        sorted(laundering_customers), round(n_seed * config.seed_laundering_share), role_rng
    )
    rest = [c for c in all_ids if c not in set(from_laundering)]
    seeded = set(from_laundering) | set(_sample(rest, n_seed - len(from_laundering), role_rng))
    lookalikes = set(
        _sample(
            [c for c in all_ids if c not in seeded],
            round(config.hard_negative_rate * len(all_ids)),
            role_rng,
        )
    )
    seed_order = sorted(seeded)
    use_opensanctions = dict.fromkeys(seed_order, False)
    n_os = round(len(seed_order) * config.seed_opensanctions_share)
    for index in role_rng.choice(len(seed_order), size=n_os, replace=False):
        use_opensanctions[seed_order[int(index)]] = True

    rows: list[dict[str, Any]] = []
    truth: list[dict[str, Any]] = []
    name_edits: list[dict[str, Any]] = []
    used: set[str] = set()
    for entity_id, entity_type in entities.iter_rows():
        cid = customer_id(entity_id)
        person = entity_type in PERSON_ENTITY_TYPES
        customer_type = "person" if person else "organization"
        # 1. Base name: the only role-specific step.
        if cid in seeded:
            preferred = "opensanctions" if use_opensanctions[cid] else "icij"
            record = _draw(pools, preferred, customer_type, used, role_rng)
            name, applied = record.name, []
            aliases = [a for a in record.aliases if is_usable_name(a, person=person)]
            if aliases and role_rng.random() < config.seed_alias_rate:
                # A real alternative name of the source, not an edit footprint.
                name, applied = str(role_rng.choice(aliases)), ["alias"]
        elif cid in lookalikes:
            preferred = (
                "opensanctions" if role_rng.random() < config.seed_opensanctions_share else "icij"
            )
            record = _draw(pools, preferred, customer_type, used, role_rng)
            name, applied = factory.lookalike(record, person=person, rng=role_rng), ["lookalike"]
        else:
            record = factory.template(customer_type, attribute_rng)
            name = (
                factory.person_name(record.country, attribute_rng)
                if person
                else factory.organization_name(record.country, attribute_rng)
            )
            applied = []

        # 2-5. Identical for every role, drawn from a role-independent stream (ADR 006):
        # canonical order, data-entry edits, "Last, First" format, casing.
        if person:
            name = canonical_person_order(name)
        n_edits = draw_edit_count(config.name_edit_count_weights, noise_rng)
        name, edits = perturb(name, person=person, n_edits=n_edits, rng=noise_rng)
        if person and noise_rng.random() < config.person_comma_format_rate:
            name = comma_format(name)
        upper = noise_rng.random() < config.customer_uppercase_rate
        name_edits.append(
            {"customer_id": cid, "edits_drawn": n_edits, "edits": edits, "upper": upper}
        )
        applied = [*applied, *edits]

        # Identical for every role: country, dates, address, casing (ADR 006).
        country, copied_country = factory.country(record, attribute_rng)
        copied = ["country"] if copied_country else []
        birth = None
        if person:
            birth = factory.date_of_birth(record)
            if birth is not None:
                copied.append("date_of_birth")
            else:
                birth = factory.faker_date_of_birth()
        styled = style_name(name, upper)
        rows.append(
            {
                "customer_id": cid,
                "ibm_entity_id": entity_id,
                "ibm_entity_type": entity_type,
                "customer_type": customer_type,
                "name": styled,
                "date_of_birth": birth,
                "incorporation_date": None if person else factory.incorporation_date(),
                "country": country,
                "address": factory.address(country, attribute_rng),
                "synthetic": True,
            }
        )
        if cid in seeded or cid in lookalikes:
            match = cid in seeded
            truth.append(
                {
                    "customer_id": cid,
                    "link_type": "seeded_match" if match else "hard_negative",
                    "is_match": match,
                    "source_entity_id": record.entity_id,
                    "source_dataset": record.dataset,
                    "original_name": record.name,
                    "seeded_name": styled,
                    "perturbations": applied,
                    # Seeds keep the reason they were chosen; look-alikes record actual membership.
                    "seed_reason": "laundering_account"
                    if (cid in from_laundering if match else cid in laundering_customers)
                    else "random",
                    "copied_attributes": copied,
                    "random_seed": config.random_seed,
                    "source_snapshot": source_snapshot,
                }
            )
    customers = pl.DataFrame(rows, schema=CUSTOMER_SCHEMA)
    ground_truth = pl.DataFrame(truth, schema=GROUND_TRUTH_SCHEMA).sort("customer_id")
    edits_frame = pl.DataFrame(name_edits, schema=NAME_EDITS_SCHEMA)
    return CustomerBuild(customers, ground_truth, edits_frame)
