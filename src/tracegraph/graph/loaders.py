"""Load validated staging Parquet into Neo4j (Phase 1 Step 3).

Reads only `data/processed/staging/` (ADR 003 §2). Each table is re-validated before anything is
written. Writes are parameterized `UNWIND $rows` batches with `MERGE` on stable IDs, so a repeated
load of the same stage is a no-op. If the graph holds anything the current stage does not, the
load stops and points to `make graph-reset`; nothing is deleted silently.
"""

import json
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
from neo4j import Driver, GraphDatabase, ManagedTransaction

from tracegraph.config import Settings, settings
from tracegraph.graph.schema import NODE_KEYS, SOURCE_REL_TYPES, apply_schema
from tracegraph.ingest.staging_schemas import STAGING_SCHEMAS, validate

# The only properties a SENT edge may carry. Labels and pattern membership are never among them.
SENT_PROPERTIES = (
    "transaction_id",
    "timestamp",
    "amount_paid",
    "payment_currency",
    "amount_received",
    "receiving_currency",
    "payment_format",
    "source_row",
)
ICIJ_NODE_LABELS = {
    "entity": "OffshoreEntity",
    "officer": "Officer",
    "intermediary": "Intermediary",
    "address": "Address",
    "other": "OtherEntity",
}

BANKS = """
UNWIND $rows AS row
MERGE (b:Bank {bank_id: row.bank_id})
SET b.name = row.bank_name
"""
ACCOUNTS = """
UNWIND $rows AS row
MATCH (b:Bank {bank_id: row.bank_id})
MERGE (a:Account {account_id: row.account_id})
SET a.bank_id = row.bank_id,
    a.account_number = row.account_number,
    a.ibm_entity_id = row.entity_id,
    a.ibm_entity_type = row.entity_type
MERGE (a)-[:HELD_AT]->(b)
"""
CUSTOMERS = """
UNWIND $rows AS row
MERGE (c:Customer {customer_id: row.customer_id})
SET c += row
"""
OWNS = """
UNWIND $rows AS row
MATCH (c:Customer {customer_id: row.customer_id})
MATCH (a:Account {account_id: row.account_id})
MERGE (c)-[:OWNS]->(a)
"""
SENT = """
UNWIND $rows AS row
MATCH (s:Account {account_id: row.from_account_id})
MATCH (t:Account {account_id: row.to_account_id})
MERGE (s)-[r:SENT {transaction_id: row.transaction_id}]->(t)
SET r.timestamp = row.timestamp,
    r.amount_paid = row.amount_paid,
    r.payment_currency = row.payment_currency,
    r.amount_received = row.amount_received,
    r.receiving_currency = row.receiving_currency,
    r.payment_format = row.payment_format,
    r.source_row = row.source_row
"""
_SOURCE_ENTITY = """
UNWIND $rows AS row
MERGE (e:SourceEntity {{entity_id: row.entity_id}})
SET e:{labels}, e += row
"""
_SOURCE_RELATIONSHIP = """
UNWIND $rows AS row
MATCH (s:SourceEntity {{entity_id: row.start_id}})
MATCH (t:SourceEntity {{entity_id: row.end_id}})
MERGE (s)-[r:{rel_type} {{relationship_id: row.relationship_id}}]->(t)
SET r += row.props
"""
# Label and relationship-type variants are built once from code constants, never from data.
OPENSANCTIONS_ENTITIES = {
    label: _SOURCE_ENTITY.format(labels=f"OpenSanctions:{label}")
    for label in ("Person", "Organization")
}
ICIJ_ENTITIES = {
    node_type: _SOURCE_ENTITY.format(labels=f"ICIJ:{label}")
    for node_type, label in ICIJ_NODE_LABELS.items()
}
SOURCE_RELATIONSHIPS = {
    rel_type: _SOURCE_RELATIONSHIP.format(rel_type=rel_type) for rel_type in SOURCE_REL_TYPES
}


class StagingMismatchError(RuntimeError):
    """The graph contains data that the current staging tables do not."""


class LoadVerificationError(RuntimeError):
    """Counts in Neo4j after the load differ from what the staging tables imply."""


@dataclass
class LoadReport:
    node_labels: dict[str, int] = field(default_factory=dict)
    relationship_types: dict[str, int] = field(default_factory=dict)
    expected_node_labels: dict[str, int] = field(default_factory=dict)
    expected_relationship_types: dict[str, int] = field(default_factory=dict)
    total_nodes: int = 0
    total_relationships: int = 0


def read_staging(staging_dir: Path) -> dict[str, pl.DataFrame]:
    """Read and re-validate every staging table (strict schemas reject label columns)."""
    tables = {}
    for name in STAGING_SCHEMAS:
        path = staging_dir / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; run `make stage` first")
        tables[name] = validate(name, pl.read_parquet(path))
    return tables


def _os_label(schema: str) -> str:
    return "Person" if schema == "Person" else "Organization"


def expected_counts(tables: dict[str, pl.DataFrame]) -> tuple[dict[str, int], dict[str, int]]:
    """Node-label and relationship-type counts the graph must have after a complete load."""
    os_entities, icij_nodes = tables["opensanctions_entities"], tables["icij_nodes"]
    labels: Counter[str] = Counter(
        Bank=tables["ibm_banks"].height,
        Account=tables["ibm_accounts"].height,
        Customer=tables["customers"].height,
        SourceEntity=os_entities.height + icij_nodes.height,
        OpenSanctions=os_entities.height,
        ICIJ=icij_nodes.height,
    )
    labels.update(_os_label(s) for s in os_entities["schema"])
    labels.update(ICIJ_NODE_LABELS[t] for t in icij_nodes["node_type"])
    accounts = tables["ibm_accounts"]
    rels: Counter[str] = Counter(
        HELD_AT=accounts.height,
        OWNS=accounts["customer_id"].drop_nulls().len(),
        SENT=tables["ibm_transactions"].height,
    )
    for name in ("opensanctions_relationships", "icij_relationships"):
        rels.update(tables[name]["rel_type"].to_list())
    return dict(sorted(labels.items())), dict(sorted(rels.items()))


def _ids(driver: Driver, database: str, query: str) -> set[str]:
    records, _, _ = driver.execute_query(query, database_=database)
    return {record["id"] for record in records}


def check_graph_matches_stage(
    driver: Driver, database: str, tables: dict[str, pl.DataFrame]
) -> None:
    """Fail loudly if the graph holds IDs that are not in the current stage."""
    staged: dict[str, set[str]] = {
        "Bank": set(tables["ibm_banks"]["bank_id"]),
        "Account": set(tables["ibm_accounts"]["account_id"]),
        "Customer": set(tables["customers"]["customer_id"]),
        "SourceEntity": set(tables["opensanctions_entities"]["entity_id"])
        | set(tables["icij_nodes"]["entity_id"]),
    }
    problems = []
    for label, key in NODE_KEYS.items():
        existing = _ids(driver, database, f"MATCH (n:{label}) RETURN n.{key} AS id")
        stale = existing - staged[label]
        if stale:
            problems.append(f"{len(stale)} {label} nodes (e.g. {sorted(stale)[:3]})")
    records, _, _ = driver.execute_query(
        "MATCH (n) WHERE NOT (n:Bank OR n:Account OR n:Customer OR n:SourceEntity) "
        "RETURN count(n) AS n",
        database_=database,
    )
    if records[0]["n"]:
        problems.append(f"{records[0]['n']} nodes with labels this loader does not create")
    sent = _ids(driver, database, "MATCH ()-[r:SENT]->() RETURN r.transaction_id AS id")
    stale_sent = sent - set(tables["ibm_transactions"]["transaction_id"])
    if stale_sent:
        problems.append(f"{len(stale_sent)} SENT edges (e.g. {sorted(stale_sent)[:3]})")
    source_rels = _ids(
        driver,
        database,
        "MATCH ()-[r]->() WHERE r.relationship_id IS NOT NULL RETURN r.relationship_id AS id",
    )
    staged_rels = set(tables["opensanctions_relationships"]["relationship_id"]) | set(
        tables["icij_relationships"]["relationship_id"]
    )
    if source_rels - staged_rels:
        problems.append(f"{len(source_rels - staged_rels)} source relationships")
    if problems:
        raise StagingMismatchError(
            "The graph contains data that is not in the current staging tables: "
            + "; ".join(problems)
            + ". Nothing was written or deleted. Run `make graph-reset`, then `make ingest`."
        )


def _batches(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _write(
    driver: Driver, database: str, query: str, rows: list[dict[str, Any]], size: int
) -> None:
    def work(tx: ManagedTransaction, batch: list[dict[str, Any]]) -> None:
        tx.run(query, rows=batch).consume()

    with driver.session(database=database) as session:
        for batch in _batches(rows, size):
            # execute_write retries transient errors (deadlocks, leader changes) a bounded time.
            session.execute_write(work, batch)


def _relationship_rows(frame: pl.DataFrame) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in frame.to_dicts():
        rel_type = row.pop("rel_type")
        ids = {k: row.pop(k) for k in ("relationship_id", "start_id", "end_id")}
        grouped.setdefault(rel_type, []).append(
            {**ids, "props": {"relationship_id": ids["relationship_id"], **row}}
        )
    return grouped


def write_graph(
    driver: Driver, database: str, tables: dict[str, pl.DataFrame], batch_size: int
) -> None:
    def put(query: str, rows: list[dict[str, Any]]) -> None:
        _write(driver, database, query, rows, batch_size)

    accounts = tables["ibm_accounts"]
    put(BANKS, tables["ibm_banks"].to_dicts())
    put(ACCOUNTS, accounts.to_dicts())
    put(CUSTOMERS, tables["customers"].to_dicts())
    put(OWNS, accounts.filter(pl.col("customer_id").is_not_null()).to_dicts())
    transactions = tables["ibm_transactions"].select(
        *SENT_PROPERTIES, "from_account_id", "to_account_id"
    )
    put(SENT, transactions.to_dicts())

    os_entities = tables["opensanctions_entities"].with_columns(
        pl.lit("opensanctions").alias("source")
    )
    for label, query in OPENSANCTIONS_ENTITIES.items():
        is_label = (
            pl.col("schema") == "Person" if label == "Person" else pl.col("schema") != "Person"
        )
        put(query, os_entities.filter(is_label).to_dicts())
    icij_nodes = tables["icij_nodes"].with_columns(pl.lit("icij").alias("source"))
    for node_type, query in ICIJ_ENTITIES.items():
        put(query, icij_nodes.filter(pl.col("node_type") == node_type).to_dicts())

    for name in ("opensanctions_relationships", "icij_relationships"):
        for rel_type, rows in _relationship_rows(tables[name]).items():
            put(SOURCE_RELATIONSHIPS[rel_type], rows)


def graph_counts(driver: Driver, database: str) -> tuple[dict[str, int], dict[str, int]]:
    labels, _, _ = driver.execute_query(
        "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n", database_=database
    )
    rels, _, _ = driver.execute_query(
        "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n", database_=database
    )
    return (
        dict(sorted((r["label"], r["n"]) for r in labels)),
        dict(sorted((r["type"], r["n"]) for r in rels)),
    )


def load_graph(driver: Driver, database: str, staging_dir: Path, batch_size: int) -> LoadReport:
    tables = read_staging(staging_dir)
    expected_labels, expected_rels = expected_counts(tables)
    apply_schema(driver, database)
    check_graph_matches_stage(driver, database, tables)
    write_graph(driver, database, tables, batch_size)

    labels, rels = graph_counts(driver, database)
    report = LoadReport(
        node_labels=labels,
        relationship_types=rels,
        expected_node_labels=expected_labels,
        expected_relationship_types=expected_rels,
        total_nodes=sum(
            tables[n].height
            for n in (
                "ibm_banks",
                "ibm_accounts",
                "customers",
                "opensanctions_entities",
                "icij_nodes",
            )
        ),
        total_relationships=sum(expected_rels.values()),
    )
    if labels != expected_labels or rels != expected_rels:
        raise LoadVerificationError(
            f"graph counts differ from staging: nodes {labels} vs {expected_labels}; "
            f"relationships {rels} vs {expected_rels}"
        )
    return report


def main(config: Settings = settings) -> int:
    driver = GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)
    )
    with driver:
        report = load_graph(
            driver, config.neo4j_database, config.staging_dir, config.graph_batch_size
        )
    output = config.processed_dir / "load_report.json"
    output.write_text(json.dumps(asdict(report), indent=2) + "\n")
    print(json.dumps(asdict(report), indent=2))
    print(f"\nLoad verified; report written to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
