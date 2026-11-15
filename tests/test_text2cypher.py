"""Static text-to-Cypher checks: what passes, and attacks that must not."""

import pytest

from tracegraph.config import Settings
from tracegraph.retrieval.text2cypher import static_problems

CONFIG = Settings(_env_file=None, qa_max_rows=50, qa_max_hops=4)

GOOD = [
    "MATCH (c:Customer {country: 'IT'}) RETURN c.customer_id AS id, 'Customer' AS type, "
    "c {.name} AS props LIMIT 10",
    "MATCH (a:Account)-[r:SENT*1..3]->(b:Account) RETURN b.account_id AS id, 'Account' AS type, "
    "{hops: size(r)} AS props LIMIT 50;",
    # Keywords inside strings and property names are not keywords.
    "MATCH (c:Customer) WHERE c.name = 'SET CREATE Ltd' AND c.start_date IS NULL "
    "RETURN c.customer_id AS id, 'Customer' AS type, c {.name} AS props LIMIT 5",
    "MATCH (e:SourceEntity)-[r:OFFICER_OF*2]-(x) RETURN x.entity_id AS id, 'SourceEntity' AS type,"
    " x {.name} AS props LIMIT 5",
]

BAD = {
    "write": "MATCH (c:Customer) SET c.name = 'x' RETURN c.customer_id AS id LIMIT 1",
    "create": "CREATE (n:Customer) RETURN n.customer_id AS id LIMIT 1",
    "detach delete": "MATCH (n) DETACH DELETE n RETURN 1 AS id LIMIT 1",
    "merge lowercase": "merge (n:Bank {bank_id: 'x'}) return n.bank_id as id limit 1",
    "procedure": "CALL dbms.components() YIELD name RETURN name AS id LIMIT 1",
    "apoc": "CALL apoc.cypher.runWrite('CREATE (n)', {}) YIELD value RETURN 1 AS id LIMIT 1",
    "load csv": "LOAD CSV FROM 'http://x' AS row RETURN row AS id LIMIT 1",
    "second statement": "MATCH (n) RETURN n.id AS id LIMIT 1; MATCH (n) DELETE n",
    "comment hides nothing": "MATCH (n) /* LIMIT 5 */ RETURN n.id AS id",
    "no limit": "MATCH (n:Customer) RETURN n.customer_id AS id",
    "limit too big": "MATCH (n:Customer) RETURN n.customer_id AS id LIMIT 5000",
    "unbounded": "MATCH (a)-[:SENT*]->(b) RETURN b.account_id AS id LIMIT 5",
    "open upper bound": "MATCH (a)-[:SENT*2..]->(b) RETURN b.account_id AS id LIMIT 5",
    "too many hops": "MATCH (a)-[:SENT*1..9]->(b) RETURN b.account_id AS id LIMIT 5",
    "quantified path": "MATCH (a) ((x)-[:SENT]->(y))+ (b) RETURN b.account_id AS id LIMIT 5",
    "quantified relationship": "MATCH (a)-[:SENT]->{1,20}(b) RETURN b.account_id AS id LIMIT 5",
    "union": "MATCH (a) RETURN a.id AS id LIMIT 1 UNION MATCH (b) RETURN b.id AS id LIMIT 1",
    "database switch": "USE system MATCH (n) RETURN n.id AS id LIMIT 1",
}


@pytest.mark.parametrize("cypher", GOOD)
def test_valid_read_queries_pass(cypher: str) -> None:
    assert static_problems(cypher, CONFIG) == []


@pytest.mark.parametrize("name", BAD)
def test_unsafe_queries_are_rejected(name: str) -> None:
    assert static_problems(BAD[name], CONFIG), name
