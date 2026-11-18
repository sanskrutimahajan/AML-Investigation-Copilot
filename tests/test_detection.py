"""Typology detectors on hand-built edge tables (no database)."""

from datetime import datetime, timedelta

import polars as pl

from tracegraph.config import Settings
from tracegraph.detection import alerts, patterns

T0 = datetime(2022, 9, 1)


def _edges(rows: list[tuple[str, str, float]]) -> pl.DataFrame:
    """(source, target, hours after T0) -> edge table with ids t0, t1, ..."""
    return pl.DataFrame(
        {
            "transaction_id": [f"t{i}" for i in range(len(rows))],
            "source": [r[0] for r in rows],
            "target": [r[1] for r in rows],
            "timestamp": [T0 + timedelta(hours=r[2]) for r in rows],
        },
        schema={"transaction_id": pl.Utf8, "source": pl.Utf8, "target": pl.Utf8,
                "timestamp": pl.Datetime("us")},
    ).sort("timestamp", "transaction_id")  # fmt: skip


def test_fan_in_counts_distinct_senders_inside_one_window() -> None:
    edges = _edges(
        [(f"s{i}", "hub", float(i)) for i in range(5)]  # 5 senders within 4 hours
        + [("s0", "hub", 5.0)]  # a repeat sender does not count twice
        + [("late", "hub", 300.0)]  # outside any 72-hour window with the others
    )
    found = patterns.fan(edges, "in", 5, 72)
    assert found["accounts"].to_list() == [["hub"]] and found["score"].to_list() == [5]
    # Evidence is the earliest window reaching the best score, so it ends at the 5th sender.
    assert set(found["transaction_ids"][0].to_list()) == {"t0", "t1", "t2", "t3", "t4"}
    assert patterns.fan(edges, "in", 6, 72).is_empty()
    assert patterns.fan(edges, "in", 5, 2).is_empty()  # only 3 senders fit in 2 hours
    assert patterns.fan(edges, "out", 2, 72).is_empty()  # no account sends to 2 receivers


def test_fan_out_is_the_mirror_image() -> None:
    edges = _edges([("hub", f"r{i}", float(i)) for i in range(4)])
    found = patterns.fan(edges, "out", 4, 24)
    assert found["typology"].to_list() == ["FAN-OUT"] and found["score"].to_list() == [4]


def test_cycles_need_time_order_and_the_window() -> None:
    ordered = _edges([("a", "b", 1), ("b", "c", 2), ("c", "a", 3)])
    found = patterns.cycles(ordered, 3, 24)
    assert found.height == 1 and sorted(found["accounts"][0].to_list()) == ["a", "b", "c"]
    assert found["score"].to_list() == [3]
    assert patterns.cycles(ordered, 2, 24).is_empty()  # 3 hops > max_hops
    assert patterns.cycles(ordered, 3, 1.5).is_empty()  # closes after the window
    unordered = _edges([("a", "b", 1), ("b", "c", 5), ("c", "a", 3)])  # c->a before b->c
    assert patterns.cycles(unordered, 3, 24).is_empty()


def test_repeated_payments_between_the_same_accounts_give_one_cycle_alert() -> None:
    edges = _edges([("a", "b", 1), ("b", "a", 2), ("a", "b", 3), ("b", "a", 4)])
    found = patterns.cycles(edges, 4, 24)
    assert found.height == 1 and found["transaction_ids"][0].to_list() == [
        "t0",
        "t1",
    ]  # the earliest


def test_scatter_gather_counts_intermediates_that_pay_the_same_sink() -> None:
    rows = [("src", f"m{i}", float(i)) for i in range(3)]
    rows += [(f"m{i}", "sink", 10.0 + i) for i in range(3)]
    found = patterns.scatter_gather(_edges(rows), 3, 48)
    assert found["accounts"].to_list() == [["src", "sink"]] and found["score"].to_list() == [3]
    assert len(found["transaction_ids"][0]) == 6
    slow = rows[:-1] + [("m2", "sink", 200.0)]  # m2 pays the sink long after being paid
    assert patterns.scatter_gather(_edges(slow), 3, 48).is_empty()
    assert patterns.scatter_gather(_edges(slow), 2, 48)["score"].to_list() == [2]


def test_build_alerts_gives_stable_ids_and_a_summary_of_what_was_counted() -> None:
    edges = _edges([(f"s{i}", "hub", float(i)) for i in range(6)])
    config = Settings(_env_file=None, detect_fan_in_min_senders=6, detect_fan_in_window_hours=24)
    first, second = alerts.build_alerts(edges, config), alerts.build_alerts(edges, config)
    assert first["alert_id"].to_list() == second["alert_id"].to_list()
    row = first.filter(pl.col("typology") == "FAN-IN").row(0, named=True)
    assert row["alert_id"].startswith("alert-") and len(row["alert_id"]) == len("alert-") + 16
    assert row["summary"].startswith("6 distinct senders paid account hub")
    assert row["detection_thresholds"] == "min_senders=6, window_hours=24"
