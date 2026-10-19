"""Coverage of IBM laundering-pattern transactions in the loaded graph (`make pattern-coverage`).

Eval-side code: it reads the raw patterns file and the eval ground truth, which graph loaders never
do (ADR 003 §2). Per typology, it reports:
- `lines_in_file`: transaction lines in HI-Small_Patterns.txt
- `matched_to_source`: lines matched to a row of HI-Small_Trans.csv
- `unmatched`: lines with no matching row
- `kept_in_sample`: matched transactions present in the staged sample
- `loaded_as_sent`: of those, transactions present as SENT edges in Neo4j
"""

import json
import sys
from typing import Any

import polars as pl
from neo4j import GraphDatabase

from tracegraph.config import Settings, settings
from tracegraph.ingest import ibm_aml


def coverage(config: Settings) -> dict[str, Any]:
    raw = config.raw_dir / "ibm_aml"
    patterns = ibm_aml.read_patterns(raw / ibm_aml.PATTERNS_FILE)
    transactions = ibm_aml.read_transactions(raw / ibm_aml.TRANSACTIONS_FILE)
    joined = patterns.join(
        transactions.select([*ibm_aml.MATCH_COLUMNS, "transaction_id", "source_row"]),
        on=ibm_aml.MATCH_COLUMNS,
        how="left",
    )
    ambiguous = joined.group_by("pattern_line").len().filter(pl.col("len") > 1).height
    lines = joined.sort("source_row", nulls_last=True).unique("pattern_line", keep="first")

    staged = set(pl.read_parquet(config.staging_dir / "ibm_transactions.parquet")["transaction_id"])
    truth = pl.read_parquet(config.ibm_pattern_ground_truth_path)
    pattern_ids = sorted(set(truth["transaction_id"].filter(truth["pattern_id"].is_not_null())))

    driver = GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)
    )
    with driver:
        records, _, _ = driver.execute_query(
            "UNWIND $ids AS id MATCH ()-[r:SENT {transaction_id: id}]->() "
            "RETURN r.transaction_id AS id",
            ids=pattern_ids,
            database_=config.neo4j_database,
        )
    loaded = {record["id"] for record in records}

    rows = []
    for typology, group in sorted(lines.group_by("typology"), key=lambda kv: str(kv[0][0])):
        matched = group.filter(pl.col("transaction_id").is_not_null())
        ids = matched["transaction_id"].to_list()
        rows.append(
            {
                "typology": typology[0],
                "patterns": group["pattern_id"].n_unique(),
                "lines_in_file": group.height,
                "matched_to_source": matched.height,
                "unmatched": group.height - matched.height,
                "kept_in_sample": sum(i in staged for i in ids),
                "loaded_as_sent": sum(i in loaded for i in ids),
            }
        )
    totals = {key: sum(int(row[key]) for row in rows) for key in rows[0] if key != "typology"}
    return {
        "by_typology": rows,
        "totals": totals,
        "ambiguous_lines": ambiguous,
        "unique_pattern_transactions": len(pattern_ids),
        "unique_pattern_transactions_loaded": len(loaded),
    }


def main() -> int:
    result = coverage(settings)
    header = ["typology", *[k for k in result["by_typology"][0] if k != "typology"]]
    widths = [max(len(h), 14) for h in header]
    print("  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)))
    for row in [*result["by_typology"], {"typology": "TOTAL", **result["totals"]}]:
        print("  ".join(str(row[h]).ljust(w) for h, w in zip(header, widths, strict=True)))
    print(
        f"\nambiguous lines: {result['ambiguous_lines']}; unique pattern transactions: "
        f"{result['unique_pattern_transactions']} "
        f"(loaded as SENT: {result['unique_pattern_transactions_loaded']})"
    )
    out = settings.processed_dir / "pattern_coverage.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
