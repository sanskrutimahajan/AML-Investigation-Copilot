"""Alerts in the throwaway Neo4j: writer, loud rerun, reset scope, Q&A and SAR templates."""

from datetime import datetime

import polars as pl
import pytest
from neo4j import Driver

from tests.integration.conftest import DATABASE
from tracegraph.config import Settings
from tracegraph.detection import alerts, patterns
from tracegraph.graph import loaders
from tracegraph.graph.cypher_templates import (
    ALERT_SUMMARY,
    ALERTS,
    ALERTS_BY_TYPOLOGY,
    ALERTS_QUEUE,
    SAR_EVIDENCE,
    SCREEN_DETAILS,
    SUBJECT_TOTALS,
    base_params,
)
from tracegraph.retrieval.graph_retriever import run_read, to_evidence

pytestmark = pytest.mark.integration


def _alert(staged: Settings) -> pl.DataFrame:
    """One FAN-IN alert on the receiver of the first fixture transaction."""
    tx = pl.read_parquet(staged.staging_dir / "ibm_transactions.parquet").row(0, named=True)
    return pl.DataFrame(
        {
            "typology": ["FAN-IN"],
            "accounts": [[tx["to_account_id"]]],
            "score": [1],
            "window_start": [datetime(2022, 9, 1)],
            "window_end": [datetime(2022, 9, 2)],
            "transaction_ids": [[tx["transaction_id"]]],
            "detection_thresholds": ["min_senders=1, window_hours=24"],
            "summary": ["1 distinct senders paid account x"],
            "alert_id": ["alert-0123456789abcdef"],
        },
        schema={**patterns.ALERT_SCHEMA, "detection_thresholds": pl.Utf8, "summary": pl.Utf8,
                "alert_id": pl.Utf8},
    )  # fmt: skip


def test_alerts_write_once_reset_alone_and_reload_still_verifies(
    empty_graph: Driver, staged: Settings
) -> None:
    report = loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    alert = _alert(staged)
    assert alerts.write_alerts(empty_graph, DATABASE, alert, "run-1", 10) == {
        "alerts": 1,
        "flags": 1,
    }
    with pytest.raises(alerts.ExistingAlertsError, match="make alerts-reset"):
        alerts.write_alerts(empty_graph, DATABASE, alert, "run-2", 10)
    # A repeat load with alerts present still verifies (alerts are not staging data).
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    assert alerts.reset_alerts(empty_graph, DATABASE) == 1
    assert loaders.graph_counts(empty_graph, DATABASE) == (
        report.node_labels,
        report.relationship_types,
    )


def test_alert_and_sar_templates_return_citable_evidence(
    empty_graph: Driver, staged: Settings
) -> None:
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    alert = _alert(staged)
    alerts.write_alerts(empty_graph, DATABASE, alert, "run-1", 10)
    account = alert["accounts"][0][0]
    owner = pl.read_parquet(staged.staging_dir / "ibm_accounts.parquet").filter(
        pl.col("account_id") == account
    )["customer_id"][0]
    params = base_params(staged.qa_max_rows)
    for subject in (account, owner):
        found = to_evidence(
            run_read(empty_graph, DATABASE, ALERTS, {**params, "id": subject}, staged)
        )
        assert [e.id for e in found] == ["alert-0123456789abcdef"]
        assert found[0].props["flagged_accounts"] == [account]
    by_typology = {
        typology: [
            e.id
            for e in to_evidence(
                run_read(empty_graph, DATABASE, ALERTS_BY_TYPOLOGY,
                         {**params, "typology": typology}, staged)
            )
        ]
        for typology in ("FAN-IN", "CYCLE")
    }  # fmt: skip
    assert by_typology == {"FAN-IN": ["alert-0123456789abcdef"], "CYCLE": []}
    sar = to_evidence(
        run_read(empty_graph, DATABASE, SAR_EVIDENCE, {**params, "id": "alert-0123456789abcdef"},
                 staged)
    )  # fmt: skip
    assert {e.type for e in sar} == {"Alert", "Account", "SENT"}
    assert next(e for e in sar if e.type == "Account").props["owner_customer_id"] == owner
    assert [e.id for e in sar if e.type == "SENT"] == alert["transaction_ids"][0].to_list()


def test_load_edges_reads_every_transaction(empty_graph: Driver, staged: Settings) -> None:
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    edges = patterns.load_edges(empty_graph, DATABASE)
    staged_tx = pl.read_parquet(staged.staging_dir / "ibm_transactions.parquet")
    assert set(edges["transaction_id"]) == set(staged_tx["transaction_id"])
    assert edges["timestamp"].dtype == pl.Datetime("us")


def test_workbench_templates_page_count_and_screen(empty_graph: Driver, staged: Settings) -> None:
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    alerts.write_alerts(empty_graph, DATABASE, _alert(staged), "run-1", 10)
    params = base_params(staged.qa_max_rows)

    def ids(cypher: str, **extra: object) -> list[str]:
        rows = run_read(empty_graph, DATABASE, cypher, {**params, **extra}, staged)
        return [e.id for e in to_evidence(rows)]

    assert ids(ALERTS_QUEUE, typology=None, offset=0) == ["alert-0123456789abcdef"]
    assert ids(ALERTS_QUEUE, typology="CYCLE", offset=0) == []
    assert ids(ALERTS_QUEUE, typology=None, offset=1) == []
    summary = run_read(empty_graph, DATABASE, ALERT_SUMMARY, params, staged)
    assert [(r["typology"], r["alerts"], r["accounts"]) for r in summary] == [("FAN-IN", 1, 1)]

    customer = pl.read_parquet(staged.staging_dir / "customers.parquet")["customer_id"][0]
    entity = pl.read_parquet(staged.staging_dir / "opensanctions_entities.parquet")["entity_id"][0]
    found = to_evidence(
        run_read(empty_graph, DATABASE, SCREEN_DETAILS,
                 {**params, "ids": [entity, customer, "missing"]}, staged)
    )  # fmt: skip
    assert {(e.id, e.type) for e in found} == {(entity, "SourceEntity"), (customer, "Customer")}
    assert "name" in next(e for e in found if e.type == "Customer").props

    # Totals count every payment of every account the customer owns, sent and received.
    accounts = pl.read_parquet(staged.staging_dir / "ibm_accounts.parquet")
    owned = accounts.filter(pl.col("customer_id") == customer)["account_id"].to_list()
    tx = pl.read_parquet(staged.staging_dir / "ibm_transactions.parquet")
    totals = {
        r["direction"]: r["payments"]
        for r in run_read(empty_graph, DATABASE, SUBJECT_TOTALS, {**params, "id": customer}, staged)
    }
    assert totals.get("sent", 0) == tx.filter(pl.col("from_account_id").is_in(owned)).height
    assert totals.get("received", 0) == tx.filter(pl.col("to_account_id").is_in(owned)).height


def test_facts_recheck_an_alert_and_new_typologies_are_added_alongside(
    empty_graph: Driver, staged: Settings
) -> None:
    from tracegraph.graph.cypher_templates import ALERT_FACTS

    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=7)
    fan_in = _alert(staged)
    alerts.write_alerts(empty_graph, DATABASE, fan_in, "run-1", 10)
    params = {**base_params(1), "id": "alert-0123456789abcdef"}
    facts = run_read(empty_graph, DATABASE, ALERT_FACTS, params, staged)[0]["props"]
    assert facts["payments"] == 1 and facts["distinct_senders_into_flagged"] == 1
    assert facts["payments_out_of_flagged"] == 0

    layering = fan_in.with_columns(pl.lit("LAYERING").alias("typology"),
                                   pl.lit("alert-fedcba9876543210").alias("alert_id"))  # fmt: skip
    # FAN-IN already has alerts, so only the LAYERING alert is added.
    assert alerts.write_alerts(empty_graph, DATABASE, pl.concat([fan_in, layering]), "run-2",
                               10) == {"alerts": 2, "flags": 2}  # fmt: skip
    with pytest.raises(alerts.ExistingAlertsError, match="every typology"):
        alerts.write_alerts(empty_graph, DATABASE, layering, "run-3", 10)
