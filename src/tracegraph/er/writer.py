"""Write ER decisions to Neo4j (`make er-write`) and remove them (`make er-reset`) (ADR 007).

- `(:Customer)-[:SAME_AS]->(:SourceEntity)`: probability ≥ `er_match_threshold` (auto match).
- `(:Customer)-[:POSSIBLE_SAME_AS]->(:SourceEntity)`: `er_review_threshold` ≤ probability below
  that. These pairs are queued for review (Step A) or LLM adjudication (Step B).

Every edge carries `synthetic_link: true`: customers are synthetic, so a link says nothing about the
real person or company. If any ER edge already exists, the write fails loudly; the only delete is
`er-reset`, which asks you to confirm.
"""

import json
import sys
from datetime import UTC, datetime
from typing import Any

import polars as pl
from neo4j import Driver, GraphDatabase, ManagedTransaction

from tracegraph.config import Settings, settings
from tracegraph.graph.schema import ER_REL_TYPES

_WRITE = {
    rel_type: f"""
UNWIND $rows AS row
MATCH (c:Customer {{customer_id: row.customer_id}})
MATCH (e:SourceEntity {{entity_id: row.entity_id}})
MERGE (c)-[r:{rel_type} {{er_run_id: $run_id}}]->(e)
SET r.probability = row.probability,
    r.match_weight = row.match_weight,
    r.evidence = row.evidence,
    r.method = $method,
    r.synthetic_link = true
"""
    for rel_type in ER_REL_TYPES
}


class ExistingERRunError(RuntimeError):
    """ER edges from a previous run are present."""


def _write_batch(
    tx: ManagedTransaction, query: str, rows: list[dict[str, Any]], run_id: str, method: str
) -> None:
    tx.run(query, rows=rows, run_id=run_id, method=method).consume()


def er_edge_counts(driver: Driver, database: str) -> dict[str, int]:
    counts = {}
    for rel_type in ER_REL_TYPES:
        records, _, _ = driver.execute_query(
            f"MATCH ()-[r:{rel_type}]->() RETURN count(r) AS n", database_=database
        )
        counts[rel_type] = int(records[0]["n"])
    return counts


def _evidence(row: dict[str, Any]) -> str:
    return json.dumps(
        {k.removeprefix("gamma_"): v for k, v in row.items() if k.startswith("gamma_")}
    )


def write_er_edges(
    driver: Driver,
    database: str,
    predictions: pl.DataFrame,
    *,
    match_threshold: float,
    review_threshold: float,
    run_id: str,
    method: str,
    batch_size: int,
) -> dict[str, int]:
    existing = er_edge_counts(driver, database)
    if any(existing.values()):
        raise ExistingERRunError(
            f"ER edges already exist ({existing}). Nothing was written. "
            "Run `make er-reset` first if you want to replace them."
        )
    bands = {
        "SAME_AS": predictions.filter(pl.col("probability") >= match_threshold),
        "POSSIBLE_SAME_AS": predictions.filter(
            (pl.col("probability") >= review_threshold) & (pl.col("probability") < match_threshold)
        ),
    }
    with driver.session(database=database) as session:
        for rel_type, frame in bands.items():
            rows = [
                {
                    "customer_id": r["customer_id"],
                    "entity_id": r["entity_id"],
                    "probability": float(r["probability"]),
                    "match_weight": float(r["match_weight"]),
                    "evidence": _evidence(r),
                }
                for r in frame.iter_rows(named=True)
            ]
            for start in range(0, len(rows), batch_size):
                session.execute_write(
                    _write_batch, _WRITE[rel_type], rows[start : start + batch_size], run_id, method
                )
    written = er_edge_counts(driver, database)
    expected = {k: v.height for k, v in bands.items()}
    if written != expected:
        raise RuntimeError(f"ER edges written {written} differ from expected {expected}")
    return written


def reset_er_edges(driver: Driver, database: str) -> int:
    """Delete every ER edge (and nothing else). Returns the number deleted."""
    total = sum(er_edge_counts(driver, database).values())
    with driver.session(database=database) as session:
        for rel_type in ER_REL_TYPES:
            session.run(
                f"MATCH ()-[r:{rel_type}]->() CALL (r) {{ DELETE r }} IN TRANSACTIONS OF 10000 ROWS"
            ).consume()
    return total


def main(argv: list[str], config: Settings = settings) -> int:
    driver = GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)
    )
    with driver:
        if "--reset" in argv:
            if "--yes" not in argv:
                print("Refusing to delete ER edges without --yes (use `make er-reset`).")
                return 2
            print(f"Deleted {reset_er_edges(driver, config.neo4j_database)} ER edges.")
            return 0
        run_id = datetime.now(UTC).strftime("er-%Y%m%dT%H%M%SZ")
        predictions = pl.read_parquet(config.er_dir / "predictions_splink_full.parquet")
        written = write_er_edges(
            driver,
            config.neo4j_database,
            predictions,
            match_threshold=config.er_match_threshold,
            review_threshold=config.er_review_threshold,
            run_id=run_id,
            method="splink_full",
            batch_size=config.graph_batch_size,
        )
    print(f"{run_id}: wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
