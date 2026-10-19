"""Neo4j constraints and indexes for the investigation graph; safe to apply repeatedly.

Labels, relationship types and property names here are code constants (Cypher cannot take them
as parameters). No value from data or user input is ever formatted into these statements.
"""

import re

from neo4j import Driver

from tracegraph.ingest import icij, opensanctions

# Node label -> unique key property.
NODE_KEYS: dict[str, str] = {
    "Account": "account_id",
    "Bank": "bank_id",
    "Customer": "customer_id",
    "SourceEntity": "entity_id",
}
SOURCE_REL_TYPES: tuple[str, ...] = tuple(sorted(opensanctions.REL_TYPES | icij.REL_TYPES))
_IDENTIFIER = re.compile(r"[A-Z][A-Za-z_]*")


def _identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"unsafe Cypher identifier {name!r}")
    return name


def schema_statements() -> list[str]:
    statements = [
        f"CREATE CONSTRAINT {label.lower()}_{key} IF NOT EXISTS "
        f"FOR (n:{_identifier(label)}) REQUIRE n.{key} IS UNIQUE"
        for label, key in NODE_KEYS.items()
    ]
    statements.append(
        "CREATE CONSTRAINT sent_transaction_id IF NOT EXISTS "
        "FOR ()-[r:SENT]-() REQUIRE r.transaction_id IS UNIQUE"
    )
    statements += [
        f"CREATE CONSTRAINT {rel_type.lower()}_relationship_id IF NOT EXISTS "
        f"FOR ()-[r:{_identifier(rel_type)}]-() REQUIRE r.relationship_id IS UNIQUE"
        for rel_type in SOURCE_REL_TYPES
    ]
    statements += [
        "CREATE INDEX sent_timestamp IF NOT EXISTS FOR ()-[r:SENT]-() ON (r.timestamp)",
        "CREATE INDEX sent_amount_paid IF NOT EXISTS FOR ()-[r:SENT]-() ON (r.amount_paid)",
        "CREATE INDEX customer_name IF NOT EXISTS FOR (n:Customer) ON (n.name)",
        "CREATE INDEX source_entity_name IF NOT EXISTS FOR (n:SourceEntity) ON (n.name)",
        "CREATE FULLTEXT INDEX party_names IF NOT EXISTS "
        "FOR (n:Customer|SourceEntity) ON EACH [n.name, n.aliases]",
    ]
    return statements


def apply_schema(driver: Driver, database: str, timeout_seconds: int = 300) -> None:
    """Create all constraints and indexes, then wait until every index is online."""
    for statement in schema_statements():
        driver.execute_query(statement, database_=database)
    driver.execute_query(
        "CALL db.awaitIndexes($timeout)", timeout=timeout_seconds, database_=database
    )
