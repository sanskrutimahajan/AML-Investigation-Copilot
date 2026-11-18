"""The scenario library: how each detector is presented to analysts, as in a bank's transaction-
monitoring system.

A detector's code name (`FAN-IN`) stays in the engineering layer and the results files. Analysts see
a scenario: an ID, a business name, what it looks for, why it is a red flag, and what to check
next. `priority_weight` is the scenario's starting contribution to an alert's priority
(`ALERT_PRIORITY` in `graph/cypher_templates.py`): higher for patterns that are more specific to
laundering (structuring is a crime in itself in the US), lower for patterns that ordinary
businesses also produce (many customers paying one merchant looks like fan-in).

The weights are judgment, not fitted to data; ADR 017 says so.
"""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    typology: str
    name: str
    description: str
    red_flag: str
    next_steps: tuple[str, ...]
    priority_weight: int


SCENARIOS: dict[str, Scenario] = {
    s.typology: s
    for s in (
        Scenario(
            "TM-01", "FAN-IN", "Funnel account activity",
            "Many different accounts pay into one account within a short period.",
            "Funds from many sources pooled in one account, as when money mules forward "
            "deposits to a collector.",
            ("Check whether the senders are related to each other or to the customer.",
             "Compare the inflows with the customer's stated business and expected activity.",
             "Look at where the pooled money goes next."),
            15,
        ),
        Scenario(
            "TM-02", "FAN-OUT", "Rapid dispersal of funds",
            "One account pays many different accounts within a short period.",
            "Money split up and spread out so it is harder to trace.",
            ("Check whether the receivers are new or connected to each other.",
             "Find the source of the funds that were dispersed.",
             "Compare with payroll or supplier patterns that would explain it."),
            15,
        ),
        Scenario(
            "TM-03", "CYCLE", "Round-trip transfers",
            "Money leaves an account and returns to it through a chain of other accounts.",
            "Round-tripping can make illicit funds look like legitimate income.",
            ("Identify who controls each account in the loop.",
             "Check whether the amounts shrink at each hop, a sign of fees taken.",
             "Look for a business reason for money to come back."),
            25,
        ),
        Scenario(
            "TM-04", "SCATTER-GATHER", "Split-and-recombine transfers",
            "One account pays several intermediary accounts, which each pay the same final "
            "account.",
            "Hides that the money really moved from one party to another.",
            ("Check how the source and the final account are connected.",
             "Look at whether the intermediaries were opened recently or are otherwise dormant.",
             "Compare the total in with the total out."),
            25,
        ),
        Scenario(
            "TM-05", "LAYERING", "Layering through pass-through accounts",
            "Money is passed along a chain of accounts, each paying it on soon after receiving it.",
            "Each hop puts more distance between the money and its origin.",
            ("Trace the chain back to its first payment.",
             "Check how long each account holds the money.",
             "Look for changes of currency or payment method along the chain."),
            25,
        ),
        Scenario(
            "TM-06", "MULE-HUB", "Money mule account",
            "An account collects from several senders and then pays the money out.",
            "Typical of an account used as a pass-through by a money-mule network.",
            ("Check the account holder's profile, age of account and recent changes.",
             "See whether the senders also pay other suspected mule accounts.",
             "Find where the outgoing money lands."),
            20,
        ),
        Scenario(
            "TM-07", "STRUCTURING", "Cash structuring below the reporting threshold",
            "Several cash deposits into one account, each just under the amount that must be "
            "reported (10,000 USD in the US).",
            "Splitting cash to avoid a currency transaction report; in the US, structuring is "
            "itself an offence.",
            ("Check who made the deposits and at which branches.",
             "Look for the same pattern on related accounts.",
             "Review whether a currency transaction report was due."),
            30,
        ),
    )
}  # fmt: skip


def scenario_for(typology: str | None) -> Scenario | None:
    return SCENARIOS.get(typology or "")


def label(typology: str | None) -> str:
    """`TM-01 Funnel account activity`, or the code name if it has no scenario."""
    scenario = scenario_for(typology)
    return f"{scenario.scenario_id} {scenario.name}" if scenario else str(typology)


def as_dicts() -> list[dict[str, Any]]:
    return [{**asdict(s), "next_steps": list(s.next_steps)} for s in SCENARIOS.values()]


def priority_weights() -> dict[str, int]:
    return {t: s.priority_weight for t, s in SCENARIOS.items()}
