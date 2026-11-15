"""Phase 3 QA check (`make qa-check`): questions generated from staging, with gold answers.

Gold answers come from the staged Parquet files, never from the graph or the system under test.
Families (`qa_check_per_family` questions each; question seed `--seed`, default `random_seed`):
- holdings: "Who owns account <id>?" (gold: the owner) and "Which accounts does <name> hold?"
  for names unique among all customers and source entities (gold: every owned account). Correct:
  every gold ID appears in the answer.
- totals: "How much did account <id> send [between <d1> and <d2>]?". Correct: every per-currency
  total appears among the answer's numbers (within 0.5). A window with no payments falls back to
  the full range, so no question has an empty gold answer.
- paths: half are the owners at both ends of a real 2-payment chain; half are random customer pairs
  with no chain of up to `qa_max_hops` payments in either direction. Correct: the verdict matches.
- networks: "Which officers, intermediaries, addresses or related entities are linked to <ICIJ
  entity>?" for entities with a unique name and 1-10 relationships. Correct: every relationship
  is cited.
- lists (text-to-Cypher): "Which customers in <country> are organizations?" with 1-10 answers.
  Correct: the cited customer IDs equal the gold set.
- alerts: "Are there any typology alerts on account <id>?" for 5 accounts the detectors flag
  (gold: their alert IDs, recomputed by running the detectors on the staged transactions, not
  read from the graph) and 5 seed accounts they do not flag. Correct: every gold alert ID appears
  in the answer, or for an unflagged account the answer says no alert flags it.
Watchlist questions are not checked until entity resolution writes edges (Phase 2).

Reports intent accuracy, linking accuracy, answer accuracy (Wilson 95% intervals), the status mix,
regenerations and latency, with the LLM provider and model. Results go to
eval/results/qa_check_<timestamp>.json and .md.
"""

import argparse
import json
import math
import random
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from neo4j import GraphDatabase
from qdrant_client import QdrantClient

from tracegraph.config import Settings, settings
from tracegraph.detection.alerts import build_alerts
from tracegraph.er.embeddings import SentenceTransformerEmbedder
from tracegraph.er.normalize import normalize_name
from tracegraph.generation.answer import QAContext, QAResponse, answer_question
from tracegraph.generation.grounding_check import numbers
from tracegraph.graph.schema import SOURCE_REL_TYPES
from tracegraph.llm.factory import get_provider, llm_settings

RESULTS_DIR = Path(__file__).parent / "results"
WINDOW = ("2022-09-01", "2022-09-05")


def _words(name: str) -> str:
    return " ".join(sorted(normalize_name(name).split()))


def _unique_names(config: Settings) -> set[str]:
    """Customer and source names whose word set occurs exactly once across both tables."""
    staging = config.staging_dir
    names = pl.concat(
        [
            pl.read_parquet(staging / "customers.parquet").select("name"),
            pl.read_parquet(staging / "opensanctions_entities.parquet").select("name"),
            pl.read_parquet(staging / "icij_nodes.parquet").select("name").drop_nulls(),
        ]
    ).with_columns(pl.col("name").map_elements(_words, return_dtype=pl.Utf8).alias("words"))
    counts = names.group_by("words").len()
    unique = set(counts.filter(pl.col("len") == 1)["words"])
    return {n for n, w in names.select("name", "words").iter_rows() if w in unique and w}


def _reach(start: set[str], graph: dict[str, set[str]], depth: int) -> set[str]:
    """Accounts reachable from `start` in 1..depth directed payments."""
    seen, frontier = set(), deque((a, 0) for a in start)
    while frontier:
        node, d = frontier.popleft()
        if d == depth:
            continue
        for nxt in graph.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, d + 1))
    return seen


def build_questions(config: Settings, per_family: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    staging = config.staging_dir
    customers = pl.read_parquet(staging / "customers.parquet")
    accounts = pl.read_parquet(staging / "ibm_accounts.parquet")
    tx = pl.read_parquet(staging / "ibm_transactions.parquet")
    unique = _unique_names(config)
    owned = accounts.group_by("customer_id").agg(pl.col("account_id").sort())
    owner = dict(accounts.select("account_id", "customer_id").iter_rows())
    questions: list[dict[str, Any]] = []

    # holdings
    named = owned.join(customers.select("customer_id", "name"), on="customer_id").filter(
        pl.col("name").is_in(list(unique))
    )
    for i, row in enumerate(named.sample(per_family, seed=seed).iter_rows(named=True)):
        by_id = i % 2 == 0
        subject = row["account_id"][0] if by_id else row["name"]
        text = f"Who owns account {subject}?" if by_id else f"Which accounts does {subject} hold?"
        questions.append({
            "family": "holdings", "question": text, "intent": "customer_profile",
            "subjects": [row["account_id"][0] if by_id else row["customer_id"]],
            "gold": [row["customer_id"]] if by_id else sorted(row["account_id"]),
        })  # fmt: skip

    # totals
    senders = sorted(set(tx["from_account_id"]))
    for i, account in enumerate(rng.sample(senders, per_family)):
        sent = tx.filter(pl.col("from_account_id") == account)
        text = f"How much did account {account} send?"
        windowed = sent.filter(
            pl.col("timestamp")
            .dt.date()
            .is_between(
                datetime.fromisoformat(WINDOW[0]).date(), datetime.fromisoformat(WINDOW[1]).date()
            )
        )
        if i % 2 and windowed.height:
            sent = windowed
            text = f"How much did account {account} send between 1 and 5 September 2022?"
        totals = sent.group_by("payment_currency").agg(pl.col("amount_paid").sum())
        questions.append({
            "family": "totals", "question": text, "intent": "transactions", "subjects": [account],
            "gold": {c: round(t, 2) for c, t in totals.iter_rows()},
        })  # fmt: skip

    # paths
    graph: dict[str, set[str]] = {}
    for a, b in tx.select("from_account_id", "to_account_id").iter_rows():
        if a != b:
            graph.setdefault(a, set()).add(b)
    by_customer = {c: set(a) for c, a in owned.iter_rows()}
    ids = sorted(by_customer)
    edges = sorted((a, b) for a, targets in graph.items() for b in targets)
    positives: list[tuple[str, str]] = []
    while len(positives) < per_family // 2:  # the two ends of a real 2-payment chain
        first, middle = rng.choice(edges)
        if graph.get(middle):
            pair = (owner[first], owner[rng.choice(sorted(graph[middle]))])
            if pair[0] != pair[1] and pair not in positives:
                positives.append(pair)
    negatives: list[tuple[str, str]] = []
    while len(negatives) < per_family - per_family // 2:
        a, b = rng.sample(ids, 2)
        if not (_reach(by_customer[a], graph, config.qa_max_hops) & by_customer[b]) and not (
            _reach(by_customer[b], graph, config.qa_max_hops) & by_customer[a]
        ):
            negatives.append((a, b))
    for (a, b), connected in [(p, True) for p in positives] + [(n, False) for n in negatives]:
        questions.append({
            "family": "paths", "question": f"Is {a} connected to {b} through payments?",
            "intent": "money_path", "subjects": [a, b], "gold": connected,
        })  # fmt: skip

    # networks
    icij = pl.read_parquet(staging / "icij_nodes.parquet")
    rels = pl.read_parquet(staging / "icij_relationships.parquet").filter(
        pl.col("rel_type").is_in(list(SOURCE_REL_TYPES))
    )
    ends = pl.concat(
        [rels.select(pl.col("start_id").alias("entity_id"), "relationship_id"),
         rels.select(pl.col("end_id").alias("entity_id"), "relationship_id")]
    ).unique()  # fmt: skip
    per_entity = (
        ends.group_by("entity_id")
        .agg(pl.col("relationship_id").sort())
        .filter(pl.col("relationship_id").list.len().is_between(1, 10))
        .join(icij.select("entity_id", "name", "node_type"), on="entity_id")
        .filter((pl.col("node_type") == "entity") & pl.col("name").is_in(list(unique)))
        .sort("entity_id")
    )
    for row in per_entity.sample(per_family, seed=seed).iter_rows(named=True):
        questions.append({
            "family": "networks", "intent": "source_network", "subjects": [row["entity_id"]],
            "question": "Which officers, intermediaries, addresses or related entities are "
            f"linked to {row['name']}?",
            "gold": row["relationship_id"],
        })  # fmt: skip

    # lists (text-to-Cypher)
    orgs = customers.filter(pl.col("customer_type") == "organization")
    sizes = orgs.group_by("country").len().filter(pl.col("len").is_between(1, 10)).sort("country")
    for country in rng.sample(sizes["country"].to_list(), min(per_family, sizes.height)):
        gold = sorted(orgs.filter(pl.col("country") == country)["customer_id"])
        questions.append({
            "family": "lists", "intent": "other", "subjects": [],
            "question": f"Which customers with country code {country} are organizations?",
            "gold": gold,
        })  # fmt: skip

    # alerts: gold from the detectors run on staged edges with the configured thresholds
    edges = tx.select(
        "transaction_id",
        pl.col("from_account_id").alias("source"),
        pl.col("to_account_id").alias("target"),
        "timestamp",
    ).sort("timestamp", "transaction_id")
    flagging = (
        build_alerts(edges, config)
        .select("alert_id", "accounts")
        .explode("accounts")
        .group_by("accounts")
        .agg(pl.col("alert_id").sort())
        .sort("accounts")
    )
    flagged = dict(flagging.iter_rows())
    seed_accounts = sorted(
        pl.read_parquet(config.processed_dir / "ibm_sample_seeds.parquet")["account_id"]
    )
    positives = rng.sample(sorted(flagged), per_family // 2)
    negatives = rng.sample(
        [a for a in seed_accounts if a not in flagged], per_family - per_family // 2
    )
    for account in positives + negatives:
        questions.append({
            "family": "alerts", "intent": "alerts", "subjects": [account],
            "question": f"Are there any typology alerts on account {account}?",
            "gold": flagged.get(account, []),
        })  # fmt: skip
    return questions


def _cited(response: QAResponse) -> set[str]:
    return {c for s in response.sentences for c in s["citations"]}


def score(question: dict[str, Any], response: QAResponse) -> dict[str, Any]:
    family, gold = question["family"], question["gold"]
    chosen = [link["chosen"]["node_id"] for link in response.links if link["chosen"]]
    answered = response.status == "answered"
    if family == "holdings":
        correct = answered and all(g in response.answer for g in gold)
    elif family == "totals":
        found = numbers(response.answer)
        correct = answered and all(any(abs(v - n) <= 0.5 for n in found) for v in gold.values())
    elif family == "paths":
        verdict = "is connected to" in response.answer
        correct = answered and verdict == gold and ("No chain" in response.answer) != gold
    elif family == "networks":
        correct = answered and set(gold) <= _cited(response)
    elif family == "alerts":
        found = (
            all(g in response.answer for g in gold)
            if gold
            else ("No typology alert flags" in response.answer)
        )
        correct = answered and found
    else:
        correct = answered and _cited(response) == set(gold)
    return {
        "intent_correct": response.route.get("intent") == question["intent"],
        "link_correct": sorted(chosen) == sorted(question["subjects"])
        if question["subjects"]
        else None,
        "correct": bool(correct),
    }


def wilson(successes: int, n: int, z: float = 1.96) -> list[float]:
    if not n:
        return [0.0, 0.0]
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def _rate(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [r[key] for r in rows if r[key] is not None]
    hits = sum(values)
    return {"rate": round(hits / len(values), 3) if values else None, "n": len(values),
            "ci95": wilson(hits, len(values))}  # fmt: skip


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    seconds = sorted(r["seconds"] for r in rows)
    families = sorted({r["family"] for r in rows})
    return {
        "n_questions": len(rows),
        "intent_accuracy": _rate(rows, "intent_correct"),
        "link_accuracy": _rate(rows, "link_correct"),
        "answer_accuracy": _rate(rows, "correct"),
        "answer_accuracy_by_family": {
            f: _rate([r for r in rows if r["family"] == f], "correct") for f in families
        },
        "status": {
            s: sum(r["status"] == s for r in rows) for s in sorted({r["status"] for r in rows})
        },
        "regenerated": sum(r["drafts"] > 1 for r in rows),
        "latency_seconds": {
            "mean": round(sum(seconds) / len(seconds), 1),
            "p95": seconds[min(len(seconds) - 1, math.ceil(0.95 * len(seconds)) - 1)],
        },
        "not_checked": "watchlist_links: no ER edges in the graph until Phase 2 writes them",
    }


def markdown(report: dict[str, Any]) -> str:
    s, llm = report["summary"], report["llm"]
    lines = [
        f"# QA check {report['run_utc']}",
        "",
        f"LLM: {llm['provider']} / {llm['model']}. Questions: {s['n_questions']} (seed "
        f"{report['question_seed']}), generated from staging with gold answers.",
        "",
        "| Measure | Rate | n | 95% CI |",
        "| --- | --- | --- | --- |",
    ]
    for name in ("intent_accuracy", "link_accuracy", "answer_accuracy"):
        m = s[name]
        lines.append(f"| {name.replace('_', ' ')} | {m['rate']} | {m['n']} | {m['ci95']} |")
    for family, m in s["answer_accuracy_by_family"].items():
        lines.append(f"| answer accuracy: {family} | {m['rate']} | {m['n']} | {m['ci95']} |")
    lines += [
        "",
        f"Status: {s['status']}. Regenerated drafts: {s['regenerated']}. "
        f"Latency: mean {s['latency_seconds']['mean']} s, p95 {s['latency_seconds']['p95']} s.",
        f"Not checked: {s['not_checked']}.",
    ]
    return "\n".join(lines) + "\n"


def main(config: Settings = settings) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed", type=int, default=config.random_seed, help="question-set seed (default: config)"
    )
    seed = parser.parse_args().seed
    questions = build_questions(config, config.qa_check_per_family, seed)
    ctx = QAContext(
        GraphDatabase.driver(config.neo4j_uri, auth=(config.neo4j_username, config.neo4j_password)),
        config.neo4j_database,
        QdrantClient(url=config.qdrant_url),
        SentenceTransformerEmbedder(config),
        get_provider(config),
        config,
    )
    rows = []
    for i, question in enumerate(questions, start=1):
        response = answer_question(question["question"], ctx)
        row = {
            **question,
            **score(question, response),
            "status": response.status,
            "answer": response.answer,
            "route": response.route,
            "drafts": len(response.trace.get("drafts", [])),
            "seconds": response.trace["seconds"],
        }
        rows.append(row)
        print(
            f"[{i}/{len(questions)}] {question['family']}: {row['status']}, {row['correct']=}",
            flush=True,
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_utc": stamp,
        "question_seed": seed,
        "llm": llm_settings(config).describe(),
        "summary": summarize(rows),
        "questions": rows,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"qa_check_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    path.with_suffix(".md").write_text(markdown(report))
    print(markdown(report))
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
