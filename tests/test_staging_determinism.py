"""Staging is deterministic: two runs on the same inputs give byte-identical files (worklog
2026-10-20, ICIJ nondeterminism; 2026-11-15, hash-order-dependent Faker cities). Each run is a
separate process with its own PYTHONHASHSEED, so set or dict order cannot leak into the output.
Covers the IBM, OpenSanctions and ICIJ paths and every eval-side file staging writes.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent
SCRIPT = """
import sys
from pathlib import Path
sys.path.insert(0, {tests!r})
from raw_fixtures import fixture_settings, write_all
from tracegraph.ingest import stage
root = Path({root!r})
write_all(root)
# Enough ICIJ seeds that hop 2 and officer-address selection are exercised.
stage.run(fixture_settings(root, icij_seed_entity_count=3, icij_node_sample_size=21))
"""


def _stage(root: Path, hash_seed: str) -> Path:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    script = SCRIPT.format(tests=str(TESTS), root=str(root))
    subprocess.run([sys.executable, "-c", script], env=env, check=True, capture_output=True)
    return root / "processed"


def _checksums(processed: Path) -> dict[str, str]:
    return {
        str(path.relative_to(processed)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(processed.rglob("*.parquet"))
    }


def test_two_staging_runs_are_byte_identical(tmp_path: Path) -> None:
    first, second = _stage(tmp_path / "a", "1"), _stage(tmp_path / "b", "2")
    sums_first, sums_second = _checksums(first), _checksums(second)
    assert sums_first == sums_second
    expected = {
        "staging/ibm_transactions.parquet",
        "staging/opensanctions_entities.parquet",
        "staging/icij_nodes.parquet",
        "staging/icij_relationships.parquet",
        "staging/customers.parquet",
        "seed_ground_truth.parquet",
        "ibm_pattern_ground_truth.parquet",
        "ibm_sample_seeds.parquet",
        "customer_name_edits.parquet",
    }
    assert expected <= set(sums_first)

    def manifest(processed: Path) -> dict[str, object]:
        data = json.loads((processed / "staging" / "stage_manifest.json").read_text())
        data.pop("staged_utc")
        data["config"].pop("data_dir")
        return data

    assert manifest(first) == manifest(second)
