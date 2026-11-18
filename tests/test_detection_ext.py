"""Phase 4 detectors (layering, mule hub, rapid movement, structuring) on hand-built edges."""

from datetime import datetime, timedelta

import polars as pl

from tracegraph.detection import patterns

T0 = datetime(2022, 9, 1)


def _edges(rows: list[tuple[str, str, float, float, str, str]]) -> pl.DataFrame:
    """(source, target, hours after T0, amount, currency, format), ids t0, t1, ..."""
    return pl.DataFrame(
        {
            "transaction_id": [f"t{i}" for i in range(len(rows))],
            "source": [r[0] for r in rows],
            "target": [r[1] for r in rows],
            "timestamp": [T0 + timedelta(hours=r[2]) for r in rows],
            "amount_paid": [r[3] for r in rows],
            "payment_currency": [r[4] for r in rows],
            "amount_received": [r[3] for r in rows],
            "receiving_currency": [r[4] for r in rows],
            "payment_format": [r[5] for r in rows],
        },
        schema={"transaction_id": pl.Utf8, "source": pl.Utf8, "target": pl.Utf8,
                "timestamp": pl.Datetime("us"), "amount_paid": pl.Float64,
                "payment_currency": pl.Utf8, "amount_received": pl.Float64,
                "receiving_currency": pl.Utf8, "payment_format": pl.Utf8},
    ).sort("timestamp", "transaction_id")  # fmt: skip


def _usd(source: str, target: str, hours: float, amount: float = 100.0,
         fmt: str = "ACH") -> tuple[str, str, float, float, str, str]:  # fmt: skip
    return (source, target, hours, amount, "US Dollar", fmt)


def test_layering_follows_a_time_ordered_chain_paid_on_within_the_window() -> None:
    edges = _edges([_usd("a", "b", 0), _usd("b", "c", 5), _usd("c", "d", 10), _usd("d", "e", 15),
                    _usd("x", "y", 0), _usd("y", "z", 50)])  # fmt: skip
    found = patterns.layering(edges, 4, 24)
    assert found["accounts"].to_list() == [["b"]] and found["score"].to_list() == [4]
    assert found["transaction_ids"][0].to_list() == ["t0", "t1", "t2", "t3"]
    assert patterns.layering(edges, 5, 24).is_empty()
    assert patterns.layering(edges, 4, 4).is_empty()  # each hop is 5 hours apart
    # A payment earlier than the one received does not extend a chain.
    backwards = _edges([_usd("b", "c", 0), _usd("a", "b", 5)])
    assert patterns.layering(backwards, 2, 24).is_empty()


def test_mule_hub_needs_distinct_senders_then_money_out() -> None:
    gather = [_usd(f"s{i}", "hub", float(i)) for i in range(3)]
    found = patterns.mule_hub(_edges([*gather, _usd("hub", "out", 10)]), 3, 24)
    assert found["accounts"].to_list() == [["hub"]] and found["score"].to_list() == [3]
    assert "t3" in found["transaction_ids"][0].to_list()  # the outgoing payment is evidence
    assert patterns.mule_hub(_edges(gather), 3, 24).is_empty()  # nothing paid out
    assert patterns.mule_hub(_edges([*gather, _usd("hub", "out", 100)]), 3, 24).is_empty()


def test_rapid_movement_matches_same_currency_pass_throughs() -> None:
    edges = _edges([
        _usd("a", "m", 0, 1000), _usd("m", "b", 2, 950),      # 95% paid on in 2 h: counts
        _usd("c", "m", 10, 1000), _usd("m", "d", 11, 500),    # only 50% paid on: does not
        _usd("e", "m", 20, 1000), _usd("m", "e", 21, 990),    # paid back to the sender: does not
        ("f", "m", 30, 1000, "Euro", "ACH"), ("m", "g", 31, 990, "US Dollar", "ACH"),  # currency
    ])  # fmt: skip
    found = patterns.rapid_movement(edges, 1, 24, 0.8)
    assert found["accounts"].to_list() == [["m"]] and found["score"].to_list() == [1]
    assert set(found["transaction_ids"][0].to_list()) == {"t0", "t1"}
    assert patterns.rapid_movement(edges, 2, 24, 0.8).is_empty()
    assert patterns.rapid_movement(edges, 1, 1, 0.8).is_empty()  # 2 h is outside a 1 h window


def test_structuring_counts_sub_threshold_cash_in_one_window() -> None:
    near = [_usd(f"s{i}", "acct", float(i * 10), 9500.0, "Cash") for i in range(3)]
    decoys = [
        _usd("x", "acct", 5, 10_500.0, "Cash"),   # above the threshold
        _usd("y", "acct", 6, 9500.0, "Wire"),     # not cash
        _usd("z", "acct", 7, 8000.0, "Cash"),     # below the band
    ]  # fmt: skip
    found = patterns.structuring(_edges(near + decoys), 3, 72, 10_000, 0.1, "US Dollar")
    assert found["accounts"].to_list() == [["acct"]] and found["score"].to_list() == [3]
    assert patterns.structuring(_edges(near + decoys), 4, 72, 10_000, 0.1, "US Dollar").is_empty()
    assert patterns.structuring(_edges(near), 3, 15, 10_000, 0.1, "US Dollar").is_empty()
    assert patterns.structuring(_edges(near), 3, 72, 10_000, 0.1, "Euro").is_empty()
