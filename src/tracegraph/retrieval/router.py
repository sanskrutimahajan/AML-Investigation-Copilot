"""Question router: intent, mentioned names or IDs, and an optional date window (Phase 3).

Uses `generate_json` (schema, validation, one retry). Intents map to Cypher templates; `other`
goes to guarded text-to-Cypher. A mention must occur in the question (after normalization): small
models sometimes invent IDs. One re-ask, then invented mentions are dropped.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel

from tracegraph.config import Settings
from tracegraph.er.normalize import normalize_name
from tracegraph.llm.base import LLMProvider
from tracegraph.llm.structured import StructuredResult, generate_json

Intent = Literal[
    "customer_profile", "transactions", "money_path", "watchlist_links", "source_network", "other"
]

SYSTEM = """You route an anti-money-laundering investigator's question. Reply with JSON only.

intent, one of:
- customer_profile: who a customer is, or who owns an account; their accounts and banks
- transactions: payments sent or received by a customer or an account, totals, counterparties
- money_path: whether money flows between two customers or accounts (needs two mentions)
- watchlist_links: whether a customer matches a sanctions, watchlist or offshore-leaks record, or \
which customers match such a record
- source_network: officers, owners, intermediaries, addresses or relatives of a sanctions or \
offshore-leaks entity
- other: anything else, especially lists, counts or filters across many records ("which customers \
in IT are organizations?", "how many accounts does bank 10 hold?")

mentions: the customers, accounts or entities the question is about, copied character for \
character from the question. If the question gives a name, the mention is that name: never write \
an ID that is not in the question. A mention names one specific party; a description such as \
"customers in Italy" is not a mention. For money_path, the sender first. Empty if none.

start_date / end_date: an inclusive date window (YYYY-MM-DD) if the question names one, else null. \
The transaction data is from September 2022."""


class Route(BaseModel):
    intent: Intent
    mentions: list[str]
    start_date: date | None
    end_date: date | None


def _in_question(mention: str, question: str) -> bool:
    words = normalize_name(mention)
    return bool(words) and f" {words} " in f" {normalize_name(question)} "


def route(question: str, provider: LLMProvider, config: Settings) -> StructuredResult[Route]:
    result = generate_json(
        provider, question, Route, system=SYSTEM, retries=config.llm_json_retries
    )
    invented = [m for m in result.value.mentions if not _in_question(m, question)]
    if invented:
        prompt = (
            f"{question}\n\nYour previous reply listed mentions that are not in the question: "
            f"{invented}. Copy names and IDs exactly as they are written in the question."
        )
        result = generate_json(
            provider, prompt, Route, system=SYSTEM, retries=config.llm_json_retries
        )
        result.value.mentions = [m for m in result.value.mentions if _in_question(m, question)]
    return result
