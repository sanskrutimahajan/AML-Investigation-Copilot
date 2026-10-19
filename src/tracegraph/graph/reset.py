"""Delete every node and relationship in the configured Neo4j database (`make graph-reset`).

Constraints and indexes are kept. The command refuses to run without `--yes`; the Make target asks
the user to type `reset` first. Nothing else in the project deletes graph data.
"""

import sys

from neo4j import Driver, GraphDatabase

from tracegraph.config import settings


def reset_graph(driver: Driver, database: str) -> int:
    """Detach-delete all nodes in batches; returns the number of nodes deleted."""
    records, _, _ = driver.execute_query("MATCH (n) RETURN count(n) AS n", database_=database)
    total = int(records[0]["n"])
    with driver.session(database=database) as session:
        session.run(
            "MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS"
        ).consume()
    return total


def main(argv: list[str]) -> int:
    if "--yes" not in argv:
        print("Refusing to delete the graph without --yes (use `make graph-reset`).")
        return 2
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password)
    )
    with driver:
        deleted = reset_graph(driver, settings.neo4j_database)
    print(f"Deleted {deleted} nodes (and their relationships) from {settings.neo4j_uri}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
