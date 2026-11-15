"""Guarded graph retrieval: every query runs read-only, row-capped and time-limited (Phase 3).

Results become `Evidence` items keyed by citable node or edge IDs (`customer_id`, `account_id`,
`bank_id`, `transaction_id`, `entity_id`, `relationship_id`; ER edges have no ID of their own and
use `<customer_id>|<TYPE>|<entity_id>`). The answer step may cite only these IDs.
"""

from dataclasses import dataclass
from itertools import islice
from typing import Any

from neo4j import (
    READ_ACCESS,
    Driver,
    ManagedTransaction,
    NotificationMinimumSeverity,
    unit_of_work,
)
from neo4j.time import Date, DateTime, Duration, Time

from tracegraph.config import Settings


@dataclass(frozen=True)
class Evidence:
    id: str
    type: str
    props: dict[str, Any]

    def render(self) -> str:
        """One line for the prompt: `[id] type: key=value; ...` (nulls left out)."""
        fields = "; ".join(
            f"{k}={_text(v)}" for k, v in sorted(self.props.items()) if v is not None
        )
        return f"[{self.id}] {self.type}: {fields}"


def _text(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value)
    return str(value)


def _plain(value: Any) -> Any:
    """Driver values as plain Python (temporal types as ISO strings; date-times to the minute)."""
    if isinstance(value, DateTime):
        return value.to_native().isoformat(timespec="minutes")
    if isinstance(value, (Date, Time, Duration)):
        return value.iso_format()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def run_read(
    driver: Driver, database: str, cypher: str, params: dict[str, Any], config: Settings
) -> list[dict[str, Any]]:
    """Run one query in a read transaction; at most `qa_max_rows` rows are ever fetched."""

    @unit_of_work(timeout=config.qa_query_timeout_seconds)
    def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
        result = tx.run(cypher, params)
        return [_plain(record.data()) for record in islice(result, config.qa_max_rows)]

    # Templates may name ER properties before Phase 2 writes any edge; the warnings are noise here.
    with driver.session(
        database=database,
        default_access_mode=READ_ACCESS,
        notifications_min_severity=NotificationMinimumSeverity.OFF,
    ) as session:
        return session.execute_read(work)


def to_evidence(rows: list[dict[str, Any]]) -> list[Evidence]:
    """Template rows (`id`, `type`, `props`) as evidence; rows without an ID are dropped."""
    return [
        Evidence(str(row["id"]), str(row["type"]), dict(row["props"] or {}))
        for row in rows
        if row.get("id")
    ]
