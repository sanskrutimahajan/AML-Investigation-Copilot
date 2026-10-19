"""Check that manually downloaded raw data is present, well-formed, and matches its manifest.

Run with `make verify-data`. Each source directory under `data/raw/` holds a `MANIFEST.tsv` that
records the source URL, file checksums, and license. The IBM manifest is written on the first run,
because Kaggle downloads are manual; on later runs it is compared like the others.
"""

import csv
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tracegraph.config import Settings, settings
from tracegraph.ingest import ibm_aml, icij

IBM_SOURCE_URL = (
    "https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml"
)
IBM_LICENSE = "CDLA-Sharing-1.0"


@dataclass(frozen=True)
class Check:
    source: str
    item: str
    ok: bool
    detail: str = ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def first_line(path: Path) -> str:
    with path.open(encoding="utf-8") as handle:
        return handle.readline().rstrip("\r\n")


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _checksum_checks(
    source: str, manifest: Path, base: Path, filename: Callable[[dict[str, str]], str]
) -> list[Check]:
    if not manifest.exists():
        return [Check(source, manifest.name, False, "missing manifest")]
    checks = []
    for row in read_manifest(manifest):
        path = base / filename(row)
        if not path.exists():
            checks.append(Check(source, path.name, False, "listed in manifest but missing"))
            continue
        actual = sha256(path)
        ok = actual == row["sha256"]
        detail = "" if ok else f"expected {row['sha256'][:12]}…, got {actual[:12]}…"
        checks.append(Check(source, f"{path.name} sha256", ok, detail))
    return checks


def verify_ibm(raw: Path) -> list[Check]:
    files = {
        ibm_aml.TRANSACTIONS_FILE: lambda p: (
            first_line(p).split(",") == ibm_aml.TRANSACTIONS_HEADER
        ),
        ibm_aml.ACCOUNTS_FILE: lambda p: first_line(p).split(",") == ibm_aml.ACCOUNTS_HEADER,
        ibm_aml.PATTERNS_FILE: lambda p: first_line(p).startswith("BEGIN LAUNDERING ATTEMPT"),
    }
    checks = []
    for name, header_ok in files.items():
        path = raw / name
        if not path.exists():
            checks.append(Check("ibm_aml", name, False, "missing"))
        else:
            checks.append(Check("ibm_aml", f"{name} header", header_ok(path)))
    if not all(c.ok for c in checks):
        return checks

    manifest = raw / "MANIFEST.tsv"
    if not manifest.exists():
        retrieved = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with manifest.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["file", "url", "sha256", "recorded_utc", "license"])
            for name in files:
                writer.writerow([name, IBM_SOURCE_URL, sha256(raw / name), retrieved, IBM_LICENSE])
        checks.append(Check("ibm_aml", "MANIFEST.tsv", True, "written (first run)"))
    return checks + _checksum_checks("ibm_aml", manifest, raw, lambda row: row["file"])


def verify_opensanctions(raw: Path, config: Settings) -> list[Check]:
    checks = []
    for dataset in config.opensanctions_datasets:
        path = raw / f"{dataset}.entities.ftm.json"
        if not path.exists():
            checks.append(Check("opensanctions", path.name, False, "missing"))
            continue
        try:
            record = json.loads(first_line(path))
            ok = {"id", "schema", "properties"} <= record.keys()
        except json.JSONDecodeError:
            ok = False
        checks.append(Check("opensanctions", f"{path.name} FtM format", ok))
    manifest = raw / "MANIFEST.tsv"
    if manifest.exists():
        listed = {row["dataset"] for row in read_manifest(manifest)}
        for dataset in sorted(set(config.opensanctions_datasets) - listed):
            checks.append(Check("opensanctions", dataset, False, "not in MANIFEST.tsv"))
    return checks + _checksum_checks(
        "opensanctions", manifest, raw, lambda row: f"{row['dataset']}.entities.ftm.json"
    )


def verify_icij(raw: Path) -> list[Check]:
    checks = []
    csv_dir = raw / "csv"
    for name in [*icij.NODE_FILES.values(), icij.RELATIONSHIPS_FILE]:
        path = csv_dir / name
        if not path.exists():
            checks.append(Check("icij", name, False, "missing (unzip the archive into csv/)"))
            continue
        header = first_line(path).split(",")
        if name == icij.RELATIONSHIPS_FILE:
            ok = header == icij.RELATIONSHIPS_HEADER
        else:
            ok = all(column in header for column in icij.REQUIRED_NODE_COLUMNS)
        checks.append(Check("icij", f"{name} header", ok))
    return checks + _checksum_checks(
        "icij", raw / "MANIFEST.tsv", raw / "archive", lambda row: row["file"]
    )


def verify_all(config: Settings) -> list[Check]:
    raw = config.raw_dir
    return [
        *verify_ibm(raw / "ibm_aml"),
        *verify_opensanctions(raw / "opensanctions", config),
        *verify_icij(raw / "icij"),
    ]


def main() -> int:
    checks = verify_all(settings)
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        detail = f"  — {check.detail}" if check.detail else ""
        print(f"[{mark}] {check.source:<14} {check.item}{detail}")
    failed = sum(not c.ok for c in checks)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
