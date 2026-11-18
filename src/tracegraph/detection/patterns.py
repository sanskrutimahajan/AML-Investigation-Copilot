"""Typology detectors over `SENT` edges (Phase 4; ADR 010).

Each detector is a pure function of an edge table (`transaction_id`, `source`, `target`,
`timestamp`) and returns candidate alerts with one shared schema (`ALERT_SCHEMA`): the typology,
the flagged accounts, a score, the time window and the transactions that make up the evidence.

- **Fan-in / fan-out:** an account receives from (sends to) at least `k` distinct counterparties
  within a sliding window of `W` hours. Score: the most distinct counterparties seen in one window.
- **Cycle:** a simple directed cycle of 2 to `L` payments, each no earlier than the one before it,
  all within `W` hours of the first. Every member account is flagged. One alert per distinct set of
  accounts (the earliest occurrence), so repeated payments between the same accounts do not
  multiply alerts. Score: the cycle length.
- **Scatter-gather:** a source pays at least `k` distinct intermediates that each pay the same sink.
  Each intermediate pays the sink within `W` hours of being paid, and the payments into the
  intermediates fall within one `W`-hour window. Source and sink are flagged. Score: the number of
  intermediates.

Edges come from the graph through a read-only query. Labels and seed lists are never read here.
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta
from typing import Literal

import polars as pl
from neo4j import READ_ACCESS, Driver

ALERT_SCHEMA: dict[str, pl.DataType] = {
    "typology": pl.Utf8(),
    "accounts": pl.List(pl.Utf8),
    "score": pl.Int64(),
    "window_start": pl.Datetime("us"),
    "window_end": pl.Datetime("us"),
    "transaction_ids": pl.List(pl.Utf8),
}
_EDGES = """
MATCH (a:Account)-[r:SENT]->(b:Account)
RETURN r.transaction_id AS transaction_id, a.account_id AS source, b.account_id AS target,
       r.timestamp AS timestamp
"""


def load_edges(driver: Driver, database: str) -> pl.DataFrame:
    """Every SENT edge, read in one read-only transaction (internal query, no user input)."""
    with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
        rows = session.execute_read(lambda tx: [r.values() for r in tx.run(_EDGES)])
    frame = pl.DataFrame(
        rows, schema=["transaction_id", "source", "target", "timestamp"], orient="row"
    )
    return frame.with_columns(
        pl.col("timestamp").map_elements(lambda t: t.to_native(), return_dtype=pl.Datetime("us"))
    ).sort("timestamp", "transaction_id")


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema=ALERT_SCHEMA)


def fan(
    edges: pl.DataFrame, direction: Literal["in", "out"], k: int, window_hours: float
) -> pl.DataFrame:
    """Accounts with at least `k` distinct senders (`in`) or receivers (`out`) in one window."""
    hub, other = ("target", "source") if direction == "in" else ("source", "target")
    period = f"{round(window_hours * 60)}m"
    windows = (
        edges.select(
            pl.col(hub).alias("account"),
            pl.col(other).alias("party"),
            "timestamp",
            "transaction_id",
        )  # fmt: skip
        .sort("account", "timestamp")
        .rolling(index_column="timestamp", period=period, group_by="account")
        .agg(
            pl.col("party").n_unique().alias("score"),
            pl.col("timestamp").min().alias("window_start"),
            pl.col("transaction_id").alias("transaction_ids"),
        )
        .rename({"timestamp": "window_end"})
    )
    best = (
        windows.filter(pl.col("score") >= k)
        .sort("account", "score", "window_end", descending=[False, True, False])
        .group_by("account", maintain_order=True)
        .first()
    )
    return best.select(
        pl.lit(f"FAN-{direction.upper()}").alias("typology"),
        pl.concat_list("account").alias("accounts"),
        pl.col("score").cast(pl.Int64),
        "window_start",
        "window_end",
        "transaction_ids",
    ).cast(ALERT_SCHEMA)  # type: ignore[arg-type]


def cycles(edges: pl.DataFrame, max_hops: int, window_hours: float) -> pl.DataFrame:
    """Simple time-ordered cycles of 2..`max_hops` payments within `window_hours` of the first."""
    window = timedelta(hours=window_hours)
    out: dict[str, list[tuple[object, str, str]]] = defaultdict(list)
    for tid, source, target, ts in edges.iter_rows():
        out[source].append((ts, target, tid))
    times = {node: [e[0] for e in lst] for node, lst in out.items()}  # already time-sorted
    found: dict[frozenset[str], dict[str, object]] = {}
    for tid0, start, first, t0 in edges.iter_rows():
        if start == first:
            continue
        deadline = t0 + window
        stack = [(first, t0, [tid0], [start, first])]
        while stack:
            node, last, tids, nodes = stack.pop()
            lst, ts_list = out.get(node, []), times.get(node, [])
            lo, hi = bisect_left(ts_list, last), bisect_right(ts_list, deadline)
            for ts, nxt, tid in lst[lo:hi]:
                if nxt == start:
                    key = frozenset(nodes)  # edges are scanned in time order: first is earliest
                    if key not in found:
                        found[key] = {
                            "typology": "CYCLE",
                            "accounts": list(nodes),
                            "score": len(tids) + 1,
                            "window_start": t0,
                            "window_end": ts,
                            "transaction_ids": [*tids, tid],
                        }
                elif nxt not in nodes and len(tids) + 1 < max_hops:
                    stack.append((nxt, ts, [*tids, tid], [*nodes, nxt]))
    if not found:
        return _empty()
    return pl.DataFrame(list(found.values()), schema=ALERT_SCHEMA)


def scatter_gather(edges: pl.DataFrame, k: int, window_hours: float) -> pl.DataFrame:
    """Source -> at least `k` distinct intermediates -> one sink, within the windows above."""
    window = timedelta(hours=window_hours)
    first = edges.select(
        pl.col("source"), pl.col("target").alias("middle"),
        pl.col("timestamp").alias("t1"), pl.col("transaction_id").alias("tid1"),
    )  # fmt: skip
    second = edges.select(
        pl.col("source").alias("middle"), pl.col("target").alias("sink"),
        pl.col("timestamp").alias("t2"), pl.col("transaction_id").alias("tid2"),
    )  # fmt: skip
    paths = (
        first.join(second, on="middle")
        .filter(
            (pl.col("sink") != pl.col("source"))
            & (pl.col("middle") != pl.col("source"))
            & (pl.col("middle") != pl.col("sink"))
            & (pl.col("t2") >= pl.col("t1"))
            & (pl.col("t2") - pl.col("t1") <= window)
        )
        .sort("source", "sink", "t1")
    )
    if paths.is_empty():
        return _empty()
    period = f"{round(window_hours * 60)}m"
    windows = (
        paths.with_columns(pl.concat_str("source", pl.lit("\x00"), "sink").alias("pair"))
        .rolling(index_column="t1", period=period, group_by="pair")
        .agg(
            pl.col("middle").n_unique().alias("score"),
            pl.col("source").first(),
            pl.col("sink").first(),
            pl.col("t1").min().alias("window_start"),
            pl.col("t2").max().alias("window_end"),
            pl.col("tid1").append(pl.col("tid2")).unique().sort().alias("transaction_ids"),
        )
    )
    best = (
        windows.filter(pl.col("score") >= k)
        .sort("pair", "score", "t1", descending=[False, True, False])
        .group_by("pair", maintain_order=True)
        .first()
    )
    return best.select(
        pl.lit("SCATTER-GATHER").alias("typology"),
        pl.concat_list("source", "sink").alias("accounts"),
        pl.col("score").cast(pl.Int64),
        "window_start",
        "window_end",
        "transaction_ids",
    ).cast(ALERT_SCHEMA)  # type: ignore[arg-type]
