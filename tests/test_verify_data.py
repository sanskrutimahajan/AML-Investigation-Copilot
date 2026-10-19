"""Raw-data verification against headers and manifests."""

from pathlib import Path

from raw_fixtures import fixture_settings, write_all

from tracegraph.ingest import verify_data


def _write_manifests(data_dir: Path) -> None:
    raw = data_dir / "raw"
    rows = ["dataset\tversion\turl\tsha256\tretrieved_utc\tlicense"]
    for name in ("list_a", "list_b"):
        digest = verify_data.sha256(raw / "opensanctions" / f"{name}.entities.ftm.json")
        rows.append(f"{name}\tv1\thttps://example.invalid\t{digest}\t2026-01-01\tCC BY-NC 4.0")
    (raw / "opensanctions" / "MANIFEST.tsv").write_text("\n".join(rows) + "\n")
    archive = raw / "icij" / "archive"
    archive.mkdir()
    (archive / "full.zip").write_bytes(b"zip")
    digest = verify_data.sha256(archive / "full.zip")
    (raw / "icij" / "MANIFEST.tsv").write_text(
        "file\turl\tlast_modified\tsha256\tretrieved_utc\tlicense\n"
        f"full.zip\thttps://example.invalid\t-\t{digest}\t2026-01-01\tODbL\n"
    )


def test_all_checks_pass_and_ibm_manifest_is_written(tmp_path: Path) -> None:
    write_all(tmp_path)
    _write_manifests(tmp_path)
    checks = verify_data.verify_all(fixture_settings(tmp_path))
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert (tmp_path / "raw" / "ibm_aml" / "MANIFEST.tsv").exists()
    # The second run compares against the manifest written by the first.
    assert all(c.ok for c in verify_data.verify_all(fixture_settings(tmp_path)))


def test_changed_file_fails_checksum(tmp_path: Path) -> None:
    write_all(tmp_path)
    _write_manifests(tmp_path)
    config = fixture_settings(tmp_path)
    verify_data.verify_all(config)
    patterns = tmp_path / "raw" / "ibm_aml" / "HI-Small_Patterns.txt"
    patterns.write_text(patterns.read_text() + "\n")
    failed = [c for c in verify_data.verify_all(config) if not c.ok]
    assert [c.item for c in failed] == ["HI-Small_Patterns.txt sha256"]


def test_missing_files_and_bad_headers_are_reported(tmp_path: Path) -> None:
    write_all(tmp_path)
    (tmp_path / "raw" / "icij" / "csv" / "relationships.csv").write_text("a,b,c\n")
    (tmp_path / "raw" / "ibm_aml" / "HI-Small_accounts.csv").unlink()
    failed = {c.item for c in verify_data.verify_all(fixture_settings(tmp_path)) if not c.ok}
    assert "HI-Small_accounts.csv" in failed
    assert "relationships.csv header" in failed
    assert "MANIFEST.tsv" in failed  # OpenSanctions and ICIJ manifests are missing
