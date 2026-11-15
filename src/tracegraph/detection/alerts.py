"""Write typology alerts to Neo4j (`make alerts`) and remove them (`make alerts-reset`) (ADR 010).

`(:Alert {alert_id, typology, summary, score, window_start, window_end, transaction_ids,
detection_thresholds, run_id})-[:FLAGS]->(:Account)`, from the four detectors in `patterns.py` with
the configured (dev-tuned) thresholds. `summary` states in words what the score counts, so neither
an analyst nor the LLM mistakes a threshold for a count. `alert_id` is a stable hash of the
typology and its transactions. Alerts are pattern matches in synthetic transactions, not findings:
they need analyst review. If alerts from a previous run exist the write fails loudly; the only
delete is `alerts-reset`, which asks you to confirm.
"""

import hashlib
import sys
from datetime import UTC, datetime

import polars as pl
from neo4j import Driver, GraphDatabase, ManagedTransaction

from tracegraph.config import Settings, settings
from tracegraph.detection import patterns

_WRITE = """
UNWIND $rows AS row
MERGE (al:Alert {alert_id: row.alert_id})
SET al.typology = row.typology,
    al.score = row.score,
    al.window_start = localdatetime(row.window_start),
    al.window_end = localdatetime(row.window_end),
    al.transaction_ids = row.transaction_ids,
    al.summary = row.summary,
    al.detection_thresholds = row.detection_thresholds,
    al.run_id = $run_id
WITH al, row
UNWIND row.accounts AS account_id
MATCH (a:Account {account_id: account_id})
MERGE (al)-[:FLAGS]->(a)
"""


class ExistingAlertsError(RuntimeError):
    """Alerts from a previous run are present."""


def build_alerts(edges: pl.DataFrame, config: Settings) -> pl.DataFrame:
    """Run all four detectors with the configured thresholds; one row per alert."""
    runs = {
        "FAN-IN": (
            patterns.fan(edges, "in", config.detect_fan_in_min_senders,
                         config.detect_fan_in_window_hours),
            f"min_senders={config.detect_fan_in_min_senders}, "
            f"window_hours={config.detect_fan_in_window_hours:g}",
        ),
        "FAN-OUT": (
            patterns.fan(edges, "out", config.detect_fan_out_min_receivers,
                         config.detect_fan_out_window_hours),
            f"min_receivers={config.detect_fan_out_min_receivers}, "
            f"window_hours={config.detect_fan_out_window_hours:g}",
        ),
        "CYCLE": (
            patterns.cycles(edges, config.detect_cycle_max_hops, config.detect_cycle_window_hours),
            f"max_hops={config.detect_cycle_max_hops}, "
            f"window_hours={config.detect_cycle_window_hours:g}",
        ),
        "SCATTER-GATHER": (
            patterns.scatter_gather(edges, config.detect_sg_min_intermediates,
                                    config.detect_sg_window_hours),
            f"min_intermediates={config.detect_sg_min_intermediates}, "
            f"window_hours={config.detect_sg_window_hours:g}",
        ),
    }  # fmt: skip
    frames = [
        alerts.with_columns(pl.lit(params).alias("detection_thresholds"))
        for alerts, params in runs.values()
    ]
    alerts = pl.concat(frames)
    alerts = alerts.with_columns(
        pl.Series("summary", [_summary(row) for row in alerts.iter_rows(named=True)])
    )
    ids = [
        "alert-" + hashlib.sha256(f"{t}|{'|'.join(sorted(tx))}".encode()).hexdigest()[:16]
        for t, tx in alerts.select("typology", "transaction_ids").iter_rows()
    ]
    return alerts.with_columns(pl.Series("alert_id", ids)).unique("alert_id", keep="first")


def _summary(alert: dict[str, object]) -> str:
    """What the alert's score counts, in words."""
    accounts, score = alert["accounts"], alert["score"]
    assert isinstance(accounts, list)
    hours = (alert["window_end"] - alert["window_start"]).total_seconds() / 3600  # type: ignore[operator]
    span = (
        f"between {alert['window_start']:%Y-%m-%d %H:%M} and {alert['window_end']:%Y-%m-%d %H:%M}"
    )
    if alert["typology"] == "FAN-IN":
        return f"{score} distinct senders paid account {accounts[0]} {span}"
    if alert["typology"] == "FAN-OUT":
        return f"account {accounts[0]} paid {score} distinct receivers {span}"
    if alert["typology"] == "CYCLE":
        return (
            f"a chain of {score} payments through {len(accounts)} accounts returned to account "
            f"{accounts[0]} {span} ({hours:.1f} hours)"
        )
    return (
        f"account {accounts[0]} paid {score} distinct intermediate accounts, each of which then "
        f"paid account {accounts[1]}, {span}"
    )


def alert_counts(driver: Driver, database: str) -> dict[str, int]:
    records, _, _ = driver.execute_query(
        "MATCH (al:Alert) OPTIONAL MATCH (al)-[f:FLAGS]->() "
        "RETURN count(DISTINCT al) AS alerts, count(f) AS flags",
        database_=database,
    )
    return {"alerts": int(records[0]["alerts"]), "flags": int(records[0]["flags"])}


def _write_batch(tx: ManagedTransaction, rows: list[dict[str, object]], run_id: str) -> None:
    tx.run(_WRITE, rows=rows, run_id=run_id).consume()


def write_alerts(
    driver: Driver, database: str, alerts: pl.DataFrame, run_id: str, batch_size: int
) -> dict[str, int]:
    existing = alert_counts(driver, database)
    if existing["alerts"]:
        raise ExistingAlertsError(
            f"{existing['alerts']} alerts already exist. Nothing was written. "
            "Run `make alerts-reset` first if you want to replace them."
        )
    rows = [
        {**row, "window_start": row["window_start"].isoformat(),
         "window_end": row["window_end"].isoformat()}
        for row in alerts.to_dicts()
    ]  # fmt: skip
    with driver.session(database=database) as session:
        for start in range(0, len(rows), batch_size):
            session.execute_write(_write_batch, rows[start : start + batch_size], run_id)
    written = alert_counts(driver, database)
    expected = {"alerts": alerts.height, "flags": int(alerts["accounts"].list.len().sum())}
    if written != expected:
        raise RuntimeError(f"alerts written {written} differ from expected {expected}")
    return written


def reset_alerts(driver: Driver, database: str) -> int:
    """Delete every Alert node and its FLAGS edges (and nothing else). Returns alerts deleted."""
    total = alert_counts(driver, database)["alerts"]
    with driver.session(database=database) as session:
        session.run(
            "MATCH (al:Alert) CALL (al) { DETACH DELETE al } IN TRANSACTIONS OF 10000 ROWS"
        ).consume()
    return total


def main(argv: list[str], config: Settings = settings) -> int:
    driver = GraphDatabase.driver(
        config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)
    )
    with driver:
        if "--reset" in argv:
            if "--yes" not in argv:
                print("Refusing to delete alerts without --yes (use `make alerts-reset`).")
                return 2
            print(f"Deleted {reset_alerts(driver, config.neo4j_database)} alerts.")
            return 0
        alerts = build_alerts(patterns.load_edges(driver, config.neo4j_database), config)
        run_id = datetime.now(UTC).strftime("alerts-%Y%m%dT%H%M%SZ")
        written = write_alerts(
            driver, config.neo4j_database, alerts, run_id, config.graph_batch_size
        )
    by_type = dict(alerts.group_by("typology").len().sort("typology").iter_rows())
    print(f"{run_id}: wrote {written}; by typology {by_type}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
