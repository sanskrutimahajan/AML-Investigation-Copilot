"""Name, date and country normalization shared by blocking, scoring and (later) entity linking.

`anyascii` (ISC) transliterates any script to ASCII, so Cyrillic or Arabic aliases become
comparable to romanized names. Legal suffixes are split off into a coarse class, so "Ltd" and
"Limited" agree, and so that suffix tokens never drive blocking.
"""

import re
from datetime import date

import jellyfish
from anyascii import anyascii

# Normalized suffix phrase -> class. Longer phrases are matched first.
LEGAL_SUFFIX_CLASSES: dict[str, str] = {
    "limited liability company": "llc",
    "limited liability partnership": "llp",
    "public limited company": "plc",
    "joint stock company": "jsc",
    "closed joint stock company": "jsc",
    "open joint stock company": "jsc",
    "s a de c v": "sa",
    "sa de cv": "sa",
    "co ltd": "ltd",
    "company limited": "ltd",
    "pte ltd": "ltd",
    "pvt ltd": "ltd",
    "private limited": "ltd",
    "limited": "ltd",
    "ltd": "ltd",
    "llc": "llc",
    "l l c": "llc",
    "llp": "llp",
    "plc": "plc",
    "inc": "inc",
    "incorporated": "inc",
    "corp": "inc",
    "corporation": "inc",
    "company": "co",
    "co": "co",
    "gmbh": "gmbh",
    "ag": "ag",
    "sa": "sa",
    "s a": "sa",
    "srl": "srl",
    "s r l": "srl",
    "bv": "bv",
    "b v": "bv",
    "nv": "nv",
    "jsc": "jsc",
    "ojsc": "jsc",
    "pjsc": "jsc",
    "cjsc": "jsc",
    "ooo": "llc",
    "oao": "jsc",
    "zao": "jsc",
    "fze": "fze",
    "fzco": "fze",
    "fzc": "fze",
    "fz llc": "fze",
}
_SUFFIXES = sorted(LEGAL_SUFFIX_CLASSES, key=lambda s: len(s.split()), reverse=True)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def fold(text: str) -> str:
    """Any script -> lowercase ASCII."""
    return anyascii(text).lower()


def normalize_name(name: str) -> str:
    """Folded, punctuation-free, single-spaced name."""
    return " ".join(_NON_WORD.sub(" ", fold(name)).split())


def split_legal_suffix(normalized: str) -> tuple[str, str | None]:
    """(core name, suffix class) for a normalized name; the core keeps at least one token."""
    tokens = normalized.split()
    for suffix in _SUFFIXES:
        parts = suffix.split()
        if len(tokens) > len(parts) and tokens[-len(parts) :] == parts:
            return " ".join(tokens[: -len(parts)]), LEGAL_SUFFIX_CLASSES[suffix]
    return normalized, None


def name_tokens(core: str) -> list[str]:
    """Distinct tokens of a core name, order-insensitive (sorted)."""
    return sorted(set(core.split()))


def phonetic_key(token: str) -> str:
    """Metaphone key; typo and transliteration variants of a token often share it."""
    return jellyfish.metaphone(token)


def parse_date(value: str | date | None) -> date | None:
    """A full ISO date, else None (year-only dates are handled by `birth_year`)."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    return None


def birth_year(value: str | date | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.year
    match = re.match(r"(\d{4})", value)
    return int(match.group(1)) if match else None
