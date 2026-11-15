"""Faker output must not depend on PYTHONHASHSEED (worklog 2026-11-15: it_IT cities did).

Two subprocesses with different hash seeds draw the same addresses and names, through
`seeded_faker`, for every locale the project uses.
"""

import os
import subprocess
import sys

SCRIPT = """
import hashlib
from tracegraph.config import Settings
from tracegraph.ingest.customer_attributes import _native_name_locales, seeded_faker

out = []
for locale in Settings(_env_file=None).faker_locales:
    fake = seeded_faker(locale, 42)
    out += [fake.street_name() for _ in range(20)] + [fake.city() for _ in range(20)]
for locale in sorted(set(_native_name_locales().values())):
    fake = seeded_faker(locale, 42)
    out += [fake.first_name() for _ in range(10)] + [fake.last_name() for _ in range(10)]
    out += [fake.name() for _ in range(10)]
    if hasattr(fake, "romanized_name"):
        out += [fake.romanized_name() for _ in range(10)]
print(hashlib.sha256("|".join(out).encode()).hexdigest())
"""


def _digest(hash_seed: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    run = subprocess.run(
        [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, check=True
    )
    return run.stdout.strip()


def test_faker_output_is_independent_of_the_hash_seed() -> None:
    assert _digest("1") == _digest("2")
