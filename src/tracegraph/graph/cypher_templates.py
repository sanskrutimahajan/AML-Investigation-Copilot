"""Parameterized Cypher templates, one per question intent (Phase 3).

Every template returns rows of (`id`, `type`, `props`), where `id` is a citable node or edge ID, and
every part ends in `LIMIT $limit`. Values arrive only as parameters. The one formatted value is the
hop cap, an int from config: Cypher cannot take a variable-length bound as a parameter.

Amounts are summed per currency, never across currencies.
"""

from dataclasses import dataclass

from tracegraph.graph.schema import SOURCE_REL_TYPES

# `$id` may name a customer or one of its accounts. Each branch is an indexed lookup.
_ACCOUNTS = """CALL () {{
  MATCH ({var}:Account {{account_id: ${param}}}) RETURN {var}
  UNION
  MATCH (:Customer {{customer_id: ${param}}})-[:OWNS]->({var}:Account) RETURN {var}
}}"""
_CUSTOMER = """CALL () {
  MATCH (c:Customer {customer_id: $id}) RETURN c
  UNION
  MATCH (c:Customer)-[:OWNS]->(:Account {account_id: $id}) RETURN c
}"""
# Inclusive date window on `r`; either end may be null.
_WINDOW = """($start IS NULL OR r.timestamp >= localdatetime($start))
  AND ($end IS NULL OR r.timestamp < localdatetime($end) + duration('P1D'))"""
_ENTITY = (
    "{.name, .source, .schema, .node_type, .topics, .datasets, .countries, .birth_dates,"
    " .jurisdiction, .source_leak, .status}"
)
_TRANSACTION = (
    "{.transaction_id, .timestamp, .amount_paid, .payment_currency, .amount_received,"
    " .receiving_currency, .payment_format,"
    " from_account: startNode(r).account_id, to_account: endNode(r).account_id"
)

CUSTOMER_PROFILE = f"""
{_CUSTOMER}
OPTIONAL MATCH (c)-[:OWNS]->(a:Account)
WITH c, count(a) AS n_accounts
RETURN c.customer_id AS id, 'Customer' AS type, c {{.*, n_accounts: n_accounts}} AS props
LIMIT $limit
UNION ALL
{_CUSTOMER}
MATCH (c)-[:OWNS]->(a:Account)-[:HELD_AT]->(b:Bank)
RETURN a.account_id AS id, 'Account' AS type,
       a {{.account_number, .bank_id, bank_name: b.name, owner_customer_id: c.customer_id}} AS props
ORDER BY id LIMIT $limit
"""

TRANSACTIONS = f"""
{_ACCOUNTS.format(var="a", param="id")}
MATCH (a)-[r:SENT]->(:Account)
WHERE {_WINDOW}
WITH a.account_id AS account, r.payment_currency AS currency, count(r) AS n,
     sum(r.amount_paid) AS total, min(r.timestamp) AS first, max(r.timestamp) AS last
RETURN account AS id, 'AccountActivity' AS type,
       {{account_id: account, direction: 'sent', currency: currency, transactions: n,
         total: round(total, 2), first: first, last: last}} AS props
ORDER BY total DESC LIMIT $limit
UNION ALL
{_ACCOUNTS.format(var="a", param="id")}
MATCH (:Account)-[r:SENT]->(a)
WHERE {_WINDOW}
WITH a.account_id AS account, r.receiving_currency AS currency, count(r) AS n,
     sum(r.amount_received) AS total, min(r.timestamp) AS first, max(r.timestamp) AS last
RETURN account AS id, 'AccountActivity' AS type,
       {{account_id: account, direction: 'received', currency: currency, transactions: n,
         total: round(total, 2), first: first, last: last}} AS props
ORDER BY total DESC LIMIT $limit
UNION ALL
{_ACCOUNTS.format(var="a", param="id")}
MATCH (a)-[r:SENT]-(:Account)
WHERE {_WINDOW}
WITH DISTINCT r
RETURN r.transaction_id AS id, 'SENT' AS type, r {_TRANSACTION}}} AS props
ORDER BY r.amount_paid DESC LIMIT $limit
"""

# Directed: money from `$id` to `$other`. The caller asks both directions.
MONEY_PATH = f"""
{_ACCOUNTS.format(var="x", param="id")}
WITH collect(x) AS xs
{_ACCOUNTS.format(var="y", param="other")}
WITH xs, collect(y) AS ys
UNWIND xs AS x
UNWIND ys AS y
WITH x, y WHERE x <> y
MATCH p = shortestPath((x)-[:SENT*1..{{hops}}]->(y))
WITH p ORDER BY length(p) LIMIT 1
UNWIND range(0, length(p) - 1) AS step
WITH relationships(p)[step] AS r, step, length(p) AS hops
RETURN r.transaction_id AS id, 'SENT' AS type,
       r {_TRANSACTION}, step: step + 1, path_length: hops}} AS props
ORDER BY step LIMIT $limit
"""

WATCHLIST_LINKS = f"""
MATCH (c:Customer)-[r:SAME_AS|POSSIBLE_SAME_AS]->(e:SourceEntity)
WHERE c.customer_id = $id OR e.entity_id = $id
RETURN c.customer_id + '|' + type(r) + '|' + e.entity_id AS id, type(r) AS type,
       {{customer_id: c.customer_id, entity_id: e.entity_id, probability: r.probability,
         method: r.method, synthetic_link: r.synthetic_link}} AS props
ORDER BY r.probability DESC LIMIT $limit
UNION ALL
MATCH (c:Customer)-[:SAME_AS|POSSIBLE_SAME_AS]->(e:SourceEntity)
WHERE c.customer_id = $id OR e.entity_id = $id
WITH DISTINCT e
RETURN e.entity_id AS id, 'SourceEntity' AS type, e {_ENTITY} AS props
LIMIT $limit
"""

SOURCE_NETWORK = f"""
MATCH (e:SourceEntity {{entity_id: $id}})
RETURN e.entity_id AS id, 'SourceEntity' AS type, e {_ENTITY} AS props
LIMIT $limit
UNION ALL
MATCH (e:SourceEntity {{entity_id: $id}})-[r]-(:SourceEntity)
WHERE type(r) IN $rel_types
RETURN r.relationship_id AS id, type(r) AS type,
       r {{.*, from_entity: startNode(r).entity_id, from_name: startNode(r).name,
          to_entity: endNode(r).entity_id, to_name: endNode(r).name}} AS props
ORDER BY id LIMIT $limit
UNION ALL
MATCH (e:SourceEntity {{entity_id: $id}})-[r]-(n:SourceEntity)
WHERE type(r) IN $rel_types
WITH DISTINCT n
RETURN n.entity_id AS id, 'SourceEntity' AS type, n {_ENTITY} AS props
ORDER BY id LIMIT $limit
"""


# Typology alerts (Phase 4) flagging a customer's accounts or one account.
ALERTS = f"""
{_ACCOUNTS.format(var="a", param="id")}
MATCH (al:Alert)-[:FLAGS]->(a)
WITH DISTINCT al
RETURN al.alert_id AS id, 'Alert' AS type,
       al {{.typology, .summary, .window_start, .window_end, .detection_thresholds,
           .transaction_ids,
           flagged_accounts: [(al)-[:FLAGS]->(x:Account) | x.account_id]}} AS props
ORDER BY al.window_start LIMIT $limit
"""

# Evidence for a SAR draft: the alert, its flagged accounts with owners, and its payments.
SAR_EVIDENCE = f"""
MATCH (al:Alert {{alert_id: $id}})
RETURN al.alert_id AS id, 'Alert' AS type,
       al {{.typology, .summary, .window_start, .window_end, .detection_thresholds}} AS props
LIMIT $limit
UNION ALL
MATCH (al:Alert {{alert_id: $id}})-[:FLAGS]->(a:Account)
OPTIONAL MATCH (c:Customer)-[:OWNS]->(a)
RETURN a.account_id AS id, 'Account' AS type,
       {{bank_id: a.bank_id, owner_customer_id: c.customer_id, owner_name: c.name,
         owner_type: c.customer_type, owner_country: c.country}} AS props
ORDER BY id LIMIT $limit
UNION ALL
MATCH (al:Alert {{alert_id: $id}})
MATCH ()-[r:SENT]->()
WHERE r.transaction_id IN al.transaction_ids
RETURN r.transaction_id AS id, 'SENT' AS type, r {_TRANSACTION}}} AS props
ORDER BY r.timestamp LIMIT $limit
"""


@dataclass(frozen=True)
class Template:
    cypher: str
    subject_labels: frozenset[str]  # node labels the linked subject may have


TEMPLATES: dict[str, Template] = {
    "customer_profile": Template(CUSTOMER_PROFILE, frozenset({"Customer", "Account"})),
    "transactions": Template(TRANSACTIONS, frozenset({"Customer", "Account"})),
    "money_path": Template(MONEY_PATH, frozenset({"Customer", "Account"})),
    "watchlist_links": Template(WATCHLIST_LINKS, frozenset({"Customer", "SourceEntity"})),
    "source_network": Template(SOURCE_NETWORK, frozenset({"SourceEntity"})),
    "alerts": Template(ALERTS, frozenset({"Customer", "Account"})),
}


def cypher_for(intent: str, max_hops: int) -> str:
    """The template's Cypher with the hop cap filled in (an int from config, never input)."""
    return TEMPLATES[intent].cypher.replace("{hops}", str(int(max_hops)))


def base_params(limit: int) -> dict[str, object]:
    return {"limit": limit, "rel_types": list(SOURCE_REL_TYPES), "start": None, "end": None}
