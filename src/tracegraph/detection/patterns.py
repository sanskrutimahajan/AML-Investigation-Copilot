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

Added in the production-shape plan (Phase 4; ADR 015), evaluated separately
(`eval/detection_ext_eval.py`):

- **Layering:** a time-ordered chain of at least `L` payments where each account pays on within `W`
  hours of being paid. The account that receives the first payment of such a chain (and keeps the
  money moving) is flagged. Score: the longest chain found from that account.
- **Mule hub (gather-scatter):** an account receives from at least `k` distinct senders within `W`
  hours and then pays money out within `W` hours of the window's end. Score: distinct senders.
- **Rapid movement:** an account pays on at least `ratio` of a received amount, in the same
  currency, within `W` hours of receiving it, at least `n` times. Score: the number of such
  pass-throughs.
- **Structuring:** an account receives at least `k` cash payments in one currency, each just under
  a reporting threshold (within `band` of it), within `W` hours. Score: the number of such payments.

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
       r.timestamp AS timestamp, r.amount_paid AS amount_paid,
       r.payment_currency AS payment_currency, r.amount_received AS amount_received,
       r.receiving_currency AS receiving_currency, r.payment_format AS payment_format
"""
EDGE_COLUMNS = ["transaction_id", "source", "target", "timestamp", "amount_paid",
                "payment_currency", "amount_received", "receiving_currency",
                "payment_format"]  # fmt: skip


def load_edges(driver: Driver, database: str) -> pl.DataFrame:
    """Every SENT edge, read in one read-only transaction (internal query, no user input)."""
    with driver.session(database=database, default_access_mode=READ_ACCESS) as session:
        rows = session.execute_read(lambda tx: [r.values() for r in tx.run(_EDGES)])
    frame = pl.DataFrame(rows, schema=EDGE_COLUMNS, orient="row")
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
    core = edges.select("transaction_id", "source", "target", "timestamp")
    for tid, source, target, ts in core.iter_rows():
        out[source].append((ts, target, tid))
    times = {node: [e[0] for e in lst] for node, lst in out.items()}  # already time-sorted
    found: dict[frozenset[str], dict[str, object]] = {}
    for tid0, start, first, t0 in core.iter_rows():
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


def _alerts(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=ALERT_SCHEMA) if rows else _empty()


def layering(edges: pl.DataFrame, min_hops: int, window_hours: float) -> pl.DataFrame:
    """Chains of at least `min_hops` payments, each paid on within `window_hours` (see above)."""
    window = timedelta(hours=window_hours)
    core = edges.select("transaction_id", "source", "target", "timestamp")
    out: dict[str, list[tuple[object, int]]] = defaultdict(list)
    rows = list(core.iter_rows())
    for i, (_, source, _, ts) in enumerate(rows):
        out[source].append((ts, i))
    times = {node: [t for t, _ in lst] for node, lst in out.items()}
    # depth[i]: payments in the longest chain starting with payment i; nxt[i]: its next payment.
    depth, nxt = [1] * len(rows), [-1] * len(rows)
    for i in range(len(rows) - 1, -1, -1):  # latest first, so every later payment is known
        _, _, target, ts = rows[i]
        lst, ts_list = out.get(target, []), times.get(target, [])
        lo, hi = bisect_left(ts_list, ts), bisect_right(ts_list, ts + window)
        for _, j in lst[lo:hi]:
            if j != i and depth[j] + 1 > depth[i]:
                depth[i], nxt[i] = depth[j] + 1, j
    best: dict[str, int] = {}
    for i, (_, _, target, _) in enumerate(rows):
        if depth[i] >= min_hops and (target not in best or depth[i] > depth[best[target]]):
            best[target] = i
    alerts = []
    for account, i in best.items():
        chain: list[int] = []
        j = i
        while j != -1 and len(chain) < depth[i]:
            chain.append(j)
            j = nxt[j]
        alerts.append({"typology": "LAYERING", "accounts": [account], "score": depth[i],
                       "window_start": rows[chain[0]][3], "window_end": rows[chain[-1]][3],
                       "transaction_ids": [rows[k][0] for k in chain]})  # fmt: skip
    return _alerts(alerts)


def mule_hub(edges: pl.DataFrame, k: int, window_hours: float) -> pl.DataFrame:
    """At least `k` distinct senders within `window_hours`, then money out within the same span."""
    gathered = fan(edges, "in", k, window_hours)
    if gathered.is_empty():
        return _empty()
    hubs = gathered.with_columns(pl.col("accounts").list.first().alias("account")).sort(
        "window_end"
    )
    outgoing = (
        edges.select(pl.col("source").alias("account"), pl.col("timestamp").alias("out_time"),
                     pl.col("transaction_id").alias("out_tid"))
        .sort("out_time")
    )  # fmt: skip
    joined = hubs.join_asof(
        outgoing,
        left_on="window_end",
        right_on="out_time",
        by="account",
        strategy="forward",
        tolerance=timedelta(hours=window_hours),
        check_sortedness=False,  # sorted by time globally, hence within each account
    ).filter(pl.col("out_tid").is_not_null())
    return joined.select(
        pl.lit("MULE-HUB").alias("typology"),
        "accounts",
        "score",
        "window_start",
        pl.col("out_time").alias("window_end"),
        pl.col("transaction_ids").list.concat(pl.concat_list("out_tid")).alias("transaction_ids"),
    ).cast(ALERT_SCHEMA)  # type: ignore[arg-type]


def rapid_movement(
    edges: pl.DataFrame, min_pass_throughs: int, window_hours: float, min_ratio: float
) -> pl.DataFrame:
    """At least `min_pass_throughs` receipts each paid on (>= `min_ratio`, same currency) within
    `window_hours`. Each receipt is matched to the account's next payment in that currency."""
    incoming = edges.select(
        pl.col("target").alias("account"), pl.col("receiving_currency").alias("currency"),
        pl.col("timestamp").alias("in_time"), pl.col("amount_received").alias("in_amount"),
        pl.col("transaction_id").alias("in_tid"), pl.col("source").alias("from_account"),
    ).sort("in_time")  # fmt: skip
    outgoing = edges.select(
        pl.col("source").alias("account"), pl.col("payment_currency").alias("currency"),
        pl.col("timestamp").alias("out_time"), pl.col("amount_paid").alias("out_amount"),
        pl.col("transaction_id").alias("out_tid"), pl.col("target").alias("to_account"),
    ).sort("out_time")  # fmt: skip
    pairs = incoming.join_asof(
        outgoing,
        left_on="in_time",
        right_on="out_time",
        by=["account", "currency"],
        strategy="forward",
        tolerance=timedelta(hours=window_hours),
        check_sortedness=False,  # sorted by time globally, hence within each group
    ).filter(
        pl.col("out_tid").is_not_null()
        & (pl.col("out_tid") != pl.col("in_tid"))
        & (pl.col("to_account") != pl.col("from_account"))
        & (pl.col("out_amount") >= min_ratio * pl.col("in_amount"))
        & (pl.col("out_amount") <= pl.col("in_amount"))
    )
    grouped = pairs.group_by("account").agg(
        pl.len().alias("score"),
        pl.col("in_time").min().alias("window_start"),
        pl.col("out_time").max().alias("window_end"),
        pl.col("in_tid").append(pl.col("out_tid")).unique().sort().alias("transaction_ids"),
    )
    return (
        grouped.filter(pl.col("score") >= min_pass_throughs)
        .sort("account")
        .select(
            pl.lit("RAPID-MOVEMENT").alias("typology"),
            pl.concat_list("account").alias("accounts"),
            pl.col("score").cast(pl.Int64),
            "window_start",
            "window_end",
            "transaction_ids",
        )
        .cast(ALERT_SCHEMA)  # type: ignore[arg-type]
    )


def structuring(
    edges: pl.DataFrame,
    min_deposits: int,
    window_hours: float,
    threshold: float,
    band: float,
    currency: str,
) -> pl.DataFrame:
    """At least `min_deposits` cash payments into one account, each in [threshold x (1 - band),
    threshold) of `currency`, within `window_hours`."""
    below = edges.filter(
        (pl.col("payment_format") == "Cash")
        & (pl.col("payment_currency") == currency)
        & (pl.col("amount_paid") >= threshold * (1 - band))
        & (pl.col("amount_paid") < threshold)
    )
    if below.is_empty():
        return _empty()
    windows = (
        below.select(pl.col("target").alias("account"), "timestamp", "transaction_id")
        .sort("account", "timestamp")
        .rolling(index_column="timestamp", period=f"{round(window_hours * 60)}m",
                 group_by="account")
        .agg(pl.len().alias("score"), pl.col("timestamp").min().alias("window_start"),
             pl.col("transaction_id").alias("transaction_ids"))
        .rename({"timestamp": "window_end"})
    )  # fmt: skip
    best = (
        windows.filter(pl.col("score") >= min_deposits)
        .sort("account", "score", "window_end", descending=[False, True, False])
        .group_by("account", maintain_order=True)
        .first()
    )
    return best.select(
        pl.lit("STRUCTURING").alias("typology"),
        pl.concat_list("account").alias("accounts"),
        pl.col("score").cast(pl.Int64),
        "window_start",
        "window_end",
        "transaction_ids",
    ).cast(ALERT_SCHEMA)  # type: ignore[arg-type]
