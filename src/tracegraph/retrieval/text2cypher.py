"""Guarded text-to-Cypher for questions no template covers (Phase 3).

The LLM writes one Cypher query. It runs only if every check passes:
1. Static checks on the text, with string literals and comments removed first: a single
   statement; no write, procedure, admin or database-switch keywords; no UNION; ends in
   `LIMIT n` with n <= `qa_max_rows`; every variable-length relationship has an upper bound
   <= `qa_max_hops`; no quantified path patterns; no case or audit-log labels, relationship types
   or properties (analyst work is not investigation evidence).
2. `EXPLAIN` on the server: the query must be read-only (type `r`), return exactly the columns
   `id`, `type`, `props`, and name no unknown label, relationship type or property (schema check).
3. It then runs like a template: read-only session, timeout, row cap (`graph_retriever.run_read`).
On a failed check the LLM is told what failed and tries once more.
"""

import re
from typing import Any

from neo4j import READ_ACCESS, Driver
from pydantic import BaseModel

from tracegraph.config import Settings
from tracegraph.graph.schema import CASE_LABELS, CASE_REL_TYPES, SOURCE_REL_TYPES
from tracegraph.llm.base import LLMProvider
from tracegraph.llm.structured import generate_json
from tracegraph.retrieval.graph_retriever import Evidence, run_read, to_evidence

FORBIDDEN = (
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH", "LOAD", "CALL",
    "USE", "ALTER", "GRANT", "DENY", "REVOKE", "SHOW", "START", "STOP", "TERMINATE", "ENABLE",
    "DISABLE", "RENAME", "UNION",
)  # fmt: skip
_UNKNOWN_SCHEMA = ("UnknownLabelWarning", "UnknownRelationshipTypeWarning", "UnknownPropertyKey")
_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`")
_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_RELATIONSHIPS = re.compile(r"\[[^\]]*\]")
_BOUNDED = re.compile(r"\*\s*(?:\d+\s*)?\.\.\s*(\d+)|\*\s*(\d+)(?!\s*\.\.)")
# `{m,n}` quantifiers, and `+` / `*` after a pattern element: `((a)-->(b))+`, `-[:R]->+`.
# Arithmetic such as `sum(x) * 2` is rejected too; the LLM is asked to rewrite it.
_QUANTIFIED = re.compile(r"\{\s*\d*\s*,\s*\d*\s*\}|[)>-]\s*[+*]")
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)
# Case and audit-log identifiers that generated Cypher may not name (also inside backticks).
PRIVATE_NAMES = frozenset({*CASE_LABELS, *CASE_REL_TYPES, "case_id", "event_id", "analyst",
                           "rationale", "sar_draft"})  # fmt: skip
_BACKTICKED = re.compile(r"`([^`]*)`")

SCHEMA = f"""Nodes (key property first):
- Customer: customer_id, name, customer_type (person|organization), date_of_birth,
  incorporation_date, country, address, synthetic
- Account: account_id, account_number, bank_id
- Bank: bank_id, name
- SourceEntity (also labeled OpenSanctions|ICIJ and Person|Organization|OffshoreEntity|Officer|
  Intermediary|Address|OtherEntity): entity_id, name, source, schema, node_type, aliases, topics,
  datasets, countries, birth_dates, jurisdiction, source_leak, status
Relationships:
- (Customer)-[:OWNS]->(Account)-[:HELD_AT]->(Bank)
- (Account)-[:SENT {{transaction_id, timestamp, amount_paid, payment_currency, amount_received,
  receiving_currency, payment_format}}]->(Account)
- (Customer)-[:SAME_AS|POSSIBLE_SAME_AS {{probability}}]->(SourceEntity)  (synthetic links)
- (SourceEntity)-[:{"|".join(SOURCE_REL_TYPES)} {{relationship_id}}]->(SourceEntity)
Values: Customer.country is an upper-case ISO code ('IT', 'GB'); SourceEntity.countries holds
lower-case codes ('ru'); customer_type is 'person' or 'organization'; currencies are names
('US Dollar', 'Euro', 'UK Pound', 'Yen', 'Bitcoin', ...); payment_format is one of 'Wire', 'ACH',
'Cheque', 'Cash', 'Credit Card', 'Bitcoin'; SourceEntity.source is 'opensanctions' or 'icij'."""

SYSTEM = """You write one read-only Neo4j Cypher query that answers an investigator's question.

{schema}

Rules:
- Return exactly three columns: `id` (the key property of the node or relationship the row is \
about), `type` (its label or relationship type as a string) and `props` (a map of the fields that \
answer the question), e.g. RETURN c.customer_id AS id, 'Customer' AS type, c {{.name, .country}} \
AS props
- Read only: MATCH, OPTIONAL MATCH, WHERE, WITH, RETURN, ORDER BY, LIMIT. No CALL, no UNION.
- End with LIMIT {max_rows} or less. Bound every variable-length relationship, at most \
*1..{max_hops}.
- Timestamps are local date-times: compare with localdatetime('2022-09-01').
- Write literal values from the question into the query.
Reply with JSON only."""


class GeneratedCypher(BaseModel):
    cypher: str


def static_problems(cypher: str, config: Settings) -> list[str]:
    """Problems found in the text itself (empty if it passes)."""
    text = cypher.strip().removesuffix(";").strip()
    bare = _COMMENTS.sub(" ", _STRINGS.sub("''", text))
    problems = []
    if ";" in bare:
        problems.append("only one statement is allowed")
    for word in FORBIDDEN:
        if re.search(rf"\b{word}\b", bare, re.IGNORECASE):
            problems.append(f"{word} is not allowed")
    limit = _LIMIT.search(bare)
    if not limit:
        problems.append("the query must end with LIMIT n")
    elif int(limit.group(1)) > config.qa_max_rows:
        problems.append(f"LIMIT must be at most {config.qa_max_rows}")
    for pattern in _RELATIONSHIPS.findall(bare):
        if "*" not in pattern:
            continue
        bound = _BOUNDED.search(pattern)
        upper = bound and int(bound.group(1) or bound.group(2))
        if not upper or upper > config.qa_max_hops:
            problems.append(
                f"variable-length relationships need an upper bound <= {config.qa_max_hops}"
            )
    named = set(re.findall(r"\b\w+\b", bare)) | set(_BACKTICKED.findall(text))
    if named & PRIVATE_NAMES:
        problems.append("case and audit-log data cannot be queried")
    if _QUANTIFIED.search(bare):
        problems.append("quantified path patterns, and + or * right after ) or ->, are not allowed")
    return problems


def explain_problems(driver: Driver, database: str, cypher: str) -> list[str]:
    """Server-side checks via EXPLAIN (nothing is executed)."""
    with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
        try:
            result = session.run("EXPLAIN " + cypher.strip().removesuffix(";"))
            keys = list(result.keys())
            summary = result.consume()
        except Exception as exc:  # syntax and semantic errors come back to the LLM
            return [f"Cypher error: {str(exc)[:300]}"]
    problems = []
    if summary.query_type != "r":
        problems.append(f"the query must be read-only (got type {summary.query_type!r})")
    if keys != ["id", "type", "props"]:
        problems.append(f"return exactly id, type, props (got {keys})")
    for note in summary.notifications or []:
        if any(code in note.get("code", "") for code in _UNKNOWN_SCHEMA):
            problems.append(note.get("description", note["code"]))
    return problems


def answer_with_cypher(
    question: str, provider: LLMProvider, driver: Driver, database: str, config: Settings
) -> tuple[list[Evidence], dict[str, Any]]:
    """(evidence, trace). Evidence is empty if no attempt passed every check."""
    system = SYSTEM.format(schema=SCHEMA, max_rows=config.qa_max_rows, max_hops=config.qa_max_hops)
    prompt = question
    trace: dict[str, Any] = {"attempts": []}
    for _ in range(2):
        cypher = generate_json(
            provider, prompt, GeneratedCypher, system=system, retries=config.llm_json_retries
        ).value.cypher
        problems = static_problems(cypher, config) or explain_problems(driver, database, cypher)
        trace["attempts"].append({"cypher": cypher, "problems": problems})
        if not problems:
            rows = run_read(driver, database, cypher.strip().removesuffix(";"), {}, config)
            return to_evidence(rows), trace
        prompt = (
            f"{question}\n\nYour previous query was rejected:\n{cypher}\nProblems: "
            + "; ".join(problems)
            + "\nWrite a corrected query."
        )
    return [], trace
