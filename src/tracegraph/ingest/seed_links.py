"""Seed synthetic customers with perturbed names from OpenSanctions/ICIJ records (ER ground truth).

Every seeded link is synthetic: it says nothing about any real person or company. The answer key is
returned separately and written only to `seed_ground_truth.parquet`; no gold `SAME_AS` edge is made.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl
from faker import Faker

from tracegraph.config import Settings

PERTURBATIONS = (
    "alias",
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


@dataclass(frozen=True)
class SourceRecord:
    entity_id: str
    dataset: str
    name: str
    aliases: tuple[str, ...] = ()
    birth_date: date | None = None
    country: str | None = None


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
    if not name or is_placeholder(name) or not _USABLE_NAME.fullmatch(ascii_fold(name)):
        return False
    if sum(ch.isalpha() for ch in name) < 3:
        return False
    return len(name.split()) >= 2 if person else True


# --- perturbations: each returns the new name, or None when it does not apply -------------------


def typo(name: str, rng: np.random.Generator) -> str | None:
    tokens = name.split()
    candidates = [i for i, t in enumerate(tokens) if len(t) >= 4 and t.isalpha()]
    if not candidates:
        return None
    i = int(rng.choice(candidates))
    token, pos = tokens[i], int(rng.integers(1, len(tokens[i]) - 1))
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
    tokens = name.split()
    if len(tokens) < 2:
        return None
    if rng.random() < 0.5:
        return f"{tokens[-1]}, {' '.join(tokens[:-1])}"
    return " ".join([tokens[-1], *tokens[:-1]])


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


def perturb(
    record: SourceRecord, *, person: bool, max_ops: int, rng: np.random.Generator
) -> tuple[str, list[str]]:
    """Apply 1..max_ops applicable perturbations; returns (name, perturbations applied)."""
    applicable = ["typo", "transliteration"]
    applicable += ["token_reorder", "initials"] if person else ["suffix_removal"]
    usable_aliases = [a for a in record.aliases if is_usable_name(a, person=person)]
    if usable_aliases:
        applicable.append("alias")
    k = int(rng.integers(1, max_ops + 1))
    chosen = [str(op) for op in rng.choice(applicable, size=min(k, len(applicable)), replace=False)]

    name, applied = record.name, []
    if "alias" in chosen:
        name = str(rng.choice(usable_aliases))
        applied.append("alias")
    for op in sorted(c for c in chosen if c != "alias"):
        result = _OPERATIONS[op](name, rng)
        if result is not None and result != name:
            name = result
            applied.append(op)
    if name == record.name:  # every chosen operation was a no-op; guarantee one real change
        for op in ("typo", "token_reorder", "initials", "transliteration"):
            result = _OPERATIONS[op](name, rng)
            if result is not None and result != name:
                return result, [op]
        raise ValueError(f"cannot perturb the name of {record.entity_id}")
    return name, applied


def lookalike(record: SourceRecord, *, person: bool, fake: Faker) -> str:
    """A similar-looking but different identity (hard negative): shared surname or lead token."""
    tokens = record.name.split()
    if person:
        first = fake.first_name()
        while first.lower() == tokens[0].lower():
            first = fake.first_name()
        return f"{first} {tokens[-1]}"
    return f"{tokens[0]} {fake.last_name()} {fake.company_suffix()}"


# --- source pools ---------------------------------------------------------------------------------


def _parse_birth_date(values: list[str]) -> date | None:
    for value in values:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
    return None


def build_pools(
    opensanctions: pl.DataFrame, icij: pl.DataFrame
) -> dict[tuple[str, str], list[SourceRecord]]:
    """Usable seed records keyed by (source dataset, customer type)."""
    pools: dict[tuple[str, str], list[SourceRecord]] = {}
    for row in opensanctions.sort("entity_id").iter_rows(named=True):
        person = row["schema"] == "Person"
        if not is_usable_name(row["name"], person=person):
            continue
        countries = [c.upper() for c in row["countries"] if len(c) == 2]
        record = SourceRecord(
            entity_id=row["entity_id"],
            dataset="opensanctions",
            name=row["name"],
            aliases=tuple(row["aliases"]),
            birth_date=_parse_birth_date(row["birth_dates"]),
            country=countries[0] if countries else None,
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
        customer_type = "person" if person else "organization"
        if is_usable_name(row["name"], person=customer_type == "person"):
            pools.setdefault(("icij", customer_type), []).append(
                SourceRecord(entity_id=row["entity_id"], dataset="icij", name=row["name"])
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


def seed_links(
    customers: pl.DataFrame,
    accounts: pl.DataFrame,
    laundering_account_ids: frozenset[str],
    opensanctions: pl.DataFrame,
    icij: pl.DataFrame,
    config: Settings,
    source_snapshot: str,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return (customers with seeded names/attributes, seed ground truth)."""
    rng = np.random.default_rng(config.random_seed + 1)
    fake = Faker("en_US")
    fake.seed_instance(config.random_seed + 1)
    pools = build_pools(opensanctions, icij)

    laundering_customers = set(
        accounts.filter(pl.col("account_id").is_in(list(laundering_account_ids)))
        .get_column("customer_id")
        .drop_nulls()
        .to_list()
    )
    all_ids = customers["customer_id"].sort().to_list()
    n_seed = round(config.seeded_link_rate * len(all_ids))
    n_laundering = round(n_seed * config.seed_laundering_share)
    from_laundering = _sample(sorted(laundering_customers), n_laundering, rng)
    rest = [c for c in all_ids if c not in set(from_laundering)]
    from_random = _sample(rest, n_seed - len(from_laundering), rng)
    chosen = set(from_laundering) | set(from_random)
    negatives = _sample(
        [c for c in all_ids if c not in chosen],
        round(config.hard_negative_rate * len(all_ids)),
        rng,
    )

    n_os = round(len(chosen) * config.seed_opensanctions_share)
    order = sorted(chosen)
    os_flags = np.zeros(len(order), dtype=bool)
    os_flags[rng.choice(len(order), size=n_os, replace=False)] = True

    by_id = {row["customer_id"]: row for row in customers.iter_rows(named=True)}
    updates: dict[str, dict[str, object]] = {}
    truth: list[dict[str, object]] = []
    used: set[str] = set()

    for customer, use_os in zip(order, os_flags, strict=True):
        row = by_id[customer]
        person = row["customer_type"] == "person"
        record = _draw(
            pools, "opensanctions" if use_os else "icij", row["customer_type"], used, rng
        )
        name, applied = perturb(
            record, person=person, max_ops=config.seed_max_perturbations, rng=rng
        )
        update: dict[str, object] = {"name": name}
        copied = []
        if person and record.birth_date is not None:
            update["date_of_birth"] = record.birth_date
            copied.append("date_of_birth")
        if record.country is not None:
            update["country"] = record.country
            copied.append("country")
        updates[customer] = update
        truth.append(
            {
                "customer_id": customer,
                "link_type": "seeded_match",
                "is_match": True,
                "source_entity_id": record.entity_id,
                "source_dataset": record.dataset,
                "original_name": record.name,
                "seeded_name": name,
                "perturbations": applied,
                "seed_reason": "laundering_account" if customer in from_laundering else "random",
                "copied_attributes": copied,
                "random_seed": config.random_seed,
                "source_snapshot": source_snapshot,
            }
        )

    for customer in negatives:
        row = by_id[customer]
        person = row["customer_type"] == "person"
        preferred = "opensanctions" if rng.random() < config.seed_opensanctions_share else "icij"
        record = _draw(pools, preferred, row["customer_type"], used, rng)
        name = lookalike(record, person=person, fake=fake)
        updates[customer] = {"name": name}
        truth.append(
            {
                "customer_id": customer,
                "link_type": "hard_negative",
                "is_match": False,
                "source_entity_id": record.entity_id,
                "source_dataset": record.dataset,
                "original_name": record.name,
                "seeded_name": name,
                "perturbations": ["lookalike"],
                "seed_reason": "laundering_account"
                if customer in laundering_customers
                else "random",
                "copied_attributes": [],
                "random_seed": config.random_seed,
                "source_snapshot": source_snapshot,
            }
        )

    seeded = pl.DataFrame([{**by_id[c], **u} for c, u in updates.items()], schema=customers.schema)
    result = customers.update(seeded, on="customer_id") if seeded.height else customers
    ground_truth = pl.DataFrame(truth, schema=GROUND_TRUTH_SCHEMA).sort("customer_id")
    return result, ground_truth
