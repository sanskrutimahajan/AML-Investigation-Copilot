"""Phase 3 retrieval on the fixture graph in the throwaway Neo4j: templates, guards, linking."""

import polars as pl
import pytest
from neo4j import Driver
from neo4j.exceptions import ClientError
from qdrant_client import QdrantClient
from test_er_pipeline import HashEmbedder

from tests.integration.conftest import DATABASE
from tracegraph.config import Settings
from tracegraph.er import writer
from tracegraph.graph import loaders
from tracegraph.graph.cypher_templates import base_params, cypher_for
from tracegraph.graph.schema import SOURCE_REL_TYPES
from tracegraph.retrieval.entity_linking import Link, link_mention
from tracegraph.retrieval.graph_retriever import Evidence, run_read, to_evidence
from tracegraph.retrieval.text2cypher import explain_problems
from tracegraph.retrieval.vector_retriever import index_names, name_records

pytestmark = pytest.mark.integration


@pytest.fixture
def graph(empty_graph: Driver, staged: Settings) -> Driver:
    loaders.load_graph(empty_graph, DATABASE, staged.staging_dir, batch_size=50)
    return empty_graph


def _run(driver: Driver, config: Settings, intent: str, **params: object) -> list[Evidence]:
    query = cypher_for(intent, config.qa_max_hops)
    rows = run_read(driver, DATABASE, query, {**base_params(config.qa_max_rows), **params}, config)
    return to_evidence(rows)


def _table(config: Settings, name: str) -> pl.DataFrame:
    return pl.read_parquet(config.staging_dir / f"{name}.parquet")


def test_reads_cannot_write(graph: Driver, staged: Settings) -> None:
    with pytest.raises(ClientError, match="read access mode"):
        run_read(graph, DATABASE, "CREATE (:Probe) RETURN 1 AS id", {}, staged)
    assert (
        run_read(graph, DATABASE, "MATCH (n:Probe) RETURN count(n) AS n", {}, staged)[0]["n"] == 0
    )


def test_transaction_totals_match_staging_per_currency(graph: Driver, staged: Settings) -> None:
    tx = _table(staged, "ibm_transactions")
    account = tx["from_account_id"][0]
    sent = tx.filter(pl.col("from_account_id") == account)
    expected = {
        currency: (round(total, 2), n)
        for currency, total, n in sent.group_by("payment_currency")
        .agg(pl.col("amount_paid").sum(), pl.len())
        .iter_rows()
    }
    evidence = _run(graph, staged, "transactions", id=account)
    got = {
        e.props["currency"]: (e.props["total"], e.props["transactions"])
        for e in evidence
        if e.type == "AccountActivity" and e.props["direction"] == "sent"
    }
    assert got == expected
    involved = tx.filter(
        (pl.col("from_account_id") == account) | (pl.col("to_account_id") == account)
    )
    assert {e.id for e in evidence if e.type == "SENT"} == set(involved["transaction_id"])


def test_customer_profile_by_customer_or_account(graph: Driver, staged: Settings) -> None:
    accounts = _table(staged, "ibm_accounts")
    customer = accounts["customer_id"][0]
    owned = set(accounts.filter(pl.col("customer_id") == customer)["account_id"])
    for subject in (customer, next(iter(owned))):
        evidence = _run(graph, staged, "customer_profile", id=subject)
        assert [e.id for e in evidence if e.type == "Customer"] == [customer]
        assert {e.id for e in evidence if e.type == "Account"} == owned
        profile = next(e for e in evidence if e.type == "Customer")
        assert profile.props["n_accounts"] == len(owned)


def test_money_path_follows_payment_direction(graph: Driver, staged: Settings) -> None:
    tx = _table(staged, "ibm_transactions").filter(
        pl.col("from_account_id") != pl.col("to_account_id")
    )
    sender, receiver = tx["from_account_id"][0], tx["to_account_id"][0]
    forward = _run(graph, staged, "money_path", id=sender, other=receiver)
    assert [e.props["path_length"] for e in forward] == [1]
    assert forward[0].props["from_account"] == sender and forward[0].props["to_account"] == receiver
    backward = sorted(
        _run(graph, staged, "money_path", id=receiver, other=sender), key=lambda e: e.props["step"]
    )
    for previous, hop in zip(backward, backward[1:], strict=False):
        assert hop.props["from_account"] == previous.props["to_account"]  # a chain, if any


def test_source_network_and_watchlist_links(graph: Driver, staged: Settings) -> None:
    rels = _table(staged, "icij_relationships")
    entity = rels["start_id"][0]
    expected = set(
        rels.filter(
            ((pl.col("start_id") == entity) | (pl.col("end_id") == entity))
            & pl.col("rel_type").is_in(list(SOURCE_REL_TYPES))
        )["relationship_id"]
    )
    network = _run(graph, staged, "source_network", id=entity)
    assert {e.id for e in network if e.type != "SourceEntity"} == expected
    assert entity in {e.id for e in network if e.type == "SourceEntity"}

    customer = _table(staged, "customers")["customer_id"][0]
    assert _run(graph, staged, "watchlist_links", id=customer) == []
    writer.write_er_edges(
        graph,
        DATABASE,
        pl.DataFrame(
            {
                "customer_id": [customer],
                "entity_id": [entity],
                "probability": [0.99],
                "match_weight": [9.0],
            }
        ),  # fmt: skip
        match_threshold=0.92,
        review_threshold=0.75,
        run_id="run-1",
        method="test",
        batch_size=10,
    )
    links = _run(graph, staged, "watchlist_links", id=customer)
    assert {e.id for e in links} == {f"{customer}|SAME_AS|{entity}", entity}
    edge = next(e for e in links if e.type == "SAME_AS")
    assert edge.props["synthetic_link"] is True and edge.props["probability"] == 0.99


def test_explain_checks_schema_and_read_only(graph: Driver) -> None:
    good = (
        "MATCH (c:Customer) RETURN c.customer_id AS id, 'Customer' AS type, c {.name} AS props "
        "LIMIT 5"
    )
    assert explain_problems(graph, DATABASE, good) == []
    assert explain_problems(graph, DATABASE, good.replace(":Customer", ":Custmer"))
    assert explain_problems(graph, DATABASE, good.replace(".name", ".nmae"))
    write = "CREATE (c:Customer) RETURN c.customer_id AS id, 'x' AS type, {} AS props LIMIT 1"
    assert any("read-only" in p for p in explain_problems(graph, DATABASE, write))


def test_linking_by_id_name_and_nonsense(graph: Driver, staged: Settings) -> None:
    client = QdrantClient(":memory:")
    index_names(client, staged.qdrant_names_collection, name_records(staged), HashEmbedder())
    customers = _table(staged, "customers")
    names = customers.group_by("name").len().filter(pl.col("len") == 1)["name"]
    row = customers.filter(pl.col("name") == names[0]).row(0, named=True)

    def link(text: str) -> Link:
        return link_mention(text, graph, DATABASE, client, HashEmbedder(), staged)

    by_id = link(row["customer_id"])
    assert by_id.chosen and by_id.chosen.node_id == row["customer_id"]
    by_name = link(row["name"].lower())
    assert by_name.chosen and by_name.chosen.node_id == row["customer_id"]
    account = _table(staged, "ibm_accounts")["account_id"][0]
    in_words = link(f"account {account}")  # routers often keep the word "account"
    assert in_words.chosen and in_words.chosen.node_id == account
    assert link("zzzz qqqq").candidates == []
    assert link("cust-FFFFFFFF").chosen is None
