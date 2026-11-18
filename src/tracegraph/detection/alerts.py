"""Write typology alerts to Neo4j (`make alerts`) and remove them (`make alerts-reset`) (ADR 010).

`(:Alert {alert_id, typology, summary, score, window_start, window_end, transaction_ids,
detection_thresholds, run_id})-[:FLAGS]->(:Account)`, from the four detectors in `patterns.py` with
the configured (dev-tuned) thresholds, plus the Phase 4 layering, mule-hub and structuring
detectors (ADR 015; rapid movement did not beat its baseline on test and writes no alerts).
`summary` states in words what the score counts, so neither
an analyst nor the LLM mistakes a threshold for a count. `alert_id` is a stable hash of the
typology and its transactions. Alerts are pattern matches in synthetic transactions, not findings:
they need analyst review. Alerts of a typology that already has alerts are never rewritten: the
write fails loudly if every typology is present, and otherwise adds only the missing typologies.
The only delete is `alerts-reset`, which asks you to confirm and refuses while cases exist.
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
        "LAYERING": (
            patterns.layering(edges, config.detect_layer_min_hops,
                              config.detect_layer_window_hours),
            f"min_hops={config.detect_layer_min_hops}, "
            f"window_hours={config.detect_layer_window_hours:g}",
        ),
        "MULE-HUB": (
            patterns.mule_hub(edges, config.detect_mule_min_senders,
                              config.detect_mule_window_hours),
            f"min_senders={config.detect_mule_min_senders}, "
            f"window_hours={config.detect_mule_window_hours:g}",
        ),
        "STRUCTURING": (
            patterns.structuring(edges, config.detect_struct_min_deposits,
                                 config.detect_struct_window_hours, config.detect_struct_threshold,
                                 config.detect_struct_band, config.detect_struct_currency),
            f"min_deposits={config.detect_struct_min_deposits}, "
            f"window_hours={config.detect_struct_window_hours:g}, "
            f"threshold={config.detect_struct_threshold:g} {config.detect_struct_currency}, "
            f"band={config.detect_struct_band:g}",
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
    if alert["typology"] == "LAYERING":
        return (
            f"account {accounts[0]} received a payment and paid it on; the chain continued for "
            f"{score} payments, each made within the window after the one before, {span}"
        )
    if alert["typology"] == "MULE-HUB":
        return (
            f"account {accounts[0]} received from {score} distinct senders and then paid money "
            f"out, {span}"
        )
    if alert["typology"] == "STRUCTURING":
        return (
            f"account {accounts[0]} received {score} cash payments just under the reporting "
            f"threshold {span}"
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
    records, _, _ = driver.execute_query(
        "MATCH (al:Alert) RETURN DISTINCT al.typology AS typology", database_=database
    )
    present = sorted(r["typology"] for r in records)
    alerts = alerts.filter(~pl.col("typology").is_in(present))
    if alerts.is_empty():
        raise ExistingAlertsError(
            f"Alerts already exist for every typology ({', '.join(present)}). Nothing was "
            "written. Run `make alerts-reset` first if you want to replace them."
        )
    before = alert_counts(driver, database)
    rows = [
        {**row, "window_start": row["window_start"].isoformat(),
         "window_end": row["window_end"].isoformat()}
        for row in alerts.to_dicts()
    ]  # fmt: skip
    with driver.session(database=database) as session:
        for start in range(0, len(rows), batch_size):
            session.execute_write(_write_batch, rows[start : start + batch_size], run_id)
    written = alert_counts(driver, database)
    expected = {"alerts": before["alerts"] + alerts.height,
                "flags": before["flags"] + int(alerts["accounts"].list.len().sum())}  # fmt: skip
    if written != expected:
        raise RuntimeError(f"alerts written {written} differ from expected {expected}")
    return written


def reset_alerts(driver: Driver, database: str) -> int:
    """Delete every Alert node and its FLAGS edges (and nothing else). Returns alerts deleted."""
    cases, _, _ = driver.execute_query("MATCH (c:Case) RETURN count(c) AS n", database_=database)
    if cases[0]["n"]:
        raise ExistingAlertsError(
            f"{cases[0]['n']} cases reference alerts; deleting alerts would cut them off from "
            "their evidence. Nothing was deleted."
        )
    total = alert_counts(driver, database)["alerts"]
    with driver.session(database=database) as session:
        session.run(
            "MATCH (al:Alert) CALL (al) { DETACH DELETE al } IN TRANSACTIONS OF 10000 ROWS"
        ).consume()
    return total


# --- Priority (ADR 017) ---------------------------------------------------------------------


def priority(weight: int, score: int, watchlist: str, others: int) -> tuple[int, str]:
    """An alert's priority (0-100) and a readable breakdown: the scenario's weight, plus strength
    (2 x score, at most 20), plus 30 for an owner's SAME_AS link or 15 for POSSIBLE_SAME_AS, plus
    5 per other alert on the owners' accounts (at most 20)."""
    strength = min(20, 2 * score)
    watch = {"match": 30, "possible match": 15}.get(watchlist, 0)
    history = min(20, 5 * others)
    total = min(100, weight + strength + watch + history)
    parts = f"scenario {weight} + strength {strength} + watchlist {watch} + history {history}"
    return total, parts


_ALERT_OWNERS = """
MATCH (al:Alert)
OPTIONAL MATCH (al)-[:FLAGS]->(:Account)<-[:OWNS]-(c:Customer)
RETURN al.alert_id AS alert_id, al.typology AS typology, coalesce(al.score, 0) AS score,
       collect(DISTINCT c.customer_id) AS owners
"""
_WATCHLIST = """
MATCH (c:Customer)-[r:SAME_AS|POSSIBLE_SAME_AS]->(:SourceEntity)
RETURN c.customer_id AS customer_id, collect(DISTINCT type(r)) AS links
"""
_SET_PRIORITY = """
UNWIND $rows AS row
MATCH (al:Alert {alert_id: row.alert_id})
SET al.priority = row.priority, al.priority_breakdown = row.breakdown
"""


def _set_priority(tx: ManagedTransaction, rows: list[dict[str, object]]) -> None:
    tx.run(_SET_PRIORITY, rows=rows).consume()


def score_alerts(driver: Driver, database: str, batch_size: int) -> dict[str, int]:
    """Compute and store every alert's priority. Safe to re-run (after new ER edges, say)."""
    from tracegraph.detection.scenarios import priority_weights

    weights = priority_weights()
    alerts, _, _ = driver.execute_query(_ALERT_OWNERS, database_=database)
    links, _, _ = driver.execute_query(_WATCHLIST, database_=database)
    status = {r["customer_id"]: ("match" if "SAME_AS" in r["links"] else "possible match")
              for r in links}  # fmt: skip
    alerts_of: dict[str, set[str]] = {}
    for r in alerts:
        for owner in r["owners"]:
            alerts_of.setdefault(owner, set()).add(r["alert_id"])
    rows = []
    for r in alerts:
        owners = r["owners"]
        watch = ("match" if any(status.get(o) == "match" for o in owners)
                 else "possible match" if any(o in status for o in owners) else "none")  # fmt: skip
        related: set[str] = set().union(*(alerts_of[o] for o in owners)) if owners else set()
        related.discard(r["alert_id"])
        total, parts = priority(weights.get(r["typology"], 10), int(r["score"]), watch,
                                len(related))  # fmt: skip
        rows.append({"alert_id": r["alert_id"], "priority": total, "breakdown": parts})
    with driver.session(database=database) as session:
        for start in range(0, len(rows), batch_size):
            session.execute_write(_set_priority, rows[start : start + batch_size])
    return {"alerts_scored": len(rows)}


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
        if "--score" in argv:
            print(score_alerts(driver, config.neo4j_database, config.graph_batch_size))
            return 0
        alerts = build_alerts(patterns.load_edges(driver, config.neo4j_database), config)
        run_id = datetime.now(UTC).strftime("alerts-%Y%m%dT%H%M%SZ")
        written = write_alerts(
            driver, config.neo4j_database, alerts, run_id, config.graph_batch_size
        )
        scored = score_alerts(driver, config.neo4j_database, config.graph_batch_size)
    by_type = dict(alerts.group_by("typology").len().sort("typology").iter_rows())
    print(f"Priorities stored: {scored}")
    print("Typologies that already had alerts were left unchanged.")
    print(f"{run_id}: wrote {written}; by typology {by_type}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
