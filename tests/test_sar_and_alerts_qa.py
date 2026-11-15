"""SAR drafts and alert answers with a scripted LLM and patched graph reads."""

from typing import Any
from unittest.mock import patch

from test_llm import ScriptedProvider

from tracegraph.config import Settings
from tracegraph.generation import answer as qa
from tracegraph.generation import sar_drafter
from tracegraph.retrieval import router
from tracegraph.retrieval.entity_linking import Candidate, Link

CONFIG = Settings(_env_file=None)
ALERT_ROWS = [
    {"id": "alert-0123456789abcdef", "type": "Alert",
     "props": {"typology": "FAN-IN", "summary": "6 distinct senders paid account 1:A"}},
    {"id": "1:A", "type": "Account", "props": {"owner_customer_id": "cust-1", "owner_name": "X"}},
    {"id": "ibm-1", "type": "SENT", "props": {"amount_paid": 250.5, "payment_currency": "Euro"}},
]  # fmt: skip


def _ctx(provider: Any) -> qa.QAContext:
    return qa.QAContext(None, "neo4j", None, None, provider, CONFIG)  # type: ignore[arg-type]


def test_sar_draft_is_labeled_grounded_and_carries_notices() -> None:
    reply = (
        '{"sentences": [{"text": "Six senders paid account 1:A, including 250.50 Euro.",'
        ' "citations": ["alert-0123456789abcdef", "ibm-1"]}]}'
    )
    with patch.object(sar_drafter, "run_read", return_value=ALERT_ROWS):
        draft = sar_drafter.draft_sar("alert-0123456789abcdef", _ctx(ScriptedProvider([reply])))
    assert draft.status == "drafted"
    assert draft.text.startswith(sar_drafter.DRAFT_LABEL)
    assert qa.SYNTHETIC_NOTICE in draft.notices and qa.ALERT_NOTICE in draft.notices


def test_sar_abstains_when_the_draft_is_never_grounded() -> None:
    bad = '{"sentences": [{"text": "They sent 9,999 Euro.", "citations": ["ibm-1"]}]}'
    with patch.object(sar_drafter, "run_read", return_value=ALERT_ROWS):
        draft = sar_drafter.draft_sar("alert-0123456789abcdef", _ctx(ScriptedProvider([bad, bad])))
    assert draft.status == "insufficient_evidence" and draft.text.startswith(
        sar_drafter.DRAFT_LABEL
    )


def test_sar_for_an_unknown_alert_makes_no_llm_call() -> None:
    provider = ScriptedProvider([])
    with patch.object(sar_drafter, "run_read", return_value=[]):
        assert sar_drafter.draft_sar("alert-ffffffffffffffff", _ctx(provider)).status == "not_found"
    assert provider.prompts == []


def _alerts_question(alert_nodes: int) -> qa.QAResponse:
    plan = router.Route(intent="alerts", mentions=["1:A"], start_date=None, end_date=None)
    link = Link("1:A", [c := Candidate("1:A", "Account", "1:A", ("id",))], c)
    with (
        patch.object(qa, "route", return_value=type("R", (), {"value": plan})()),
        patch.object(qa, "retrieve", return_value=([], [link])),
        patch.object(qa, "run_read", return_value=[{"n": alert_nodes}]),
    ):
        return qa.answer_question("Any alerts on 1:A?", _ctx(ScriptedProvider([])))


def test_no_alerts_is_a_code_written_answer_and_no_detection_run_is_said_plainly() -> None:
    none_found = _alerts_question(alert_nodes=1560)
    assert none_found.status == "answered"
    assert none_found.answer == "No typology alert flags 1:A or its accounts. [1:A]"
    not_run = _alerts_question(alert_nodes=0)
    assert (
        not_run.status == "insufficient_evidence" and "has not written any alerts" in not_run.answer
    )
