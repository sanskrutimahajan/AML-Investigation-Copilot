"""Streamlit UI (Phase 5): ask cited questions, look up typology alerts, draft SARs.

Run with `make ui` while the API is up (`make api`). The page calls the API only; every answer
and draft it shows has passed the API's grounding check.
"""

from typing import Any

import streamlit as st

from tracegraph.api.client import (
    ALERT_ID,
    STATUS_LABELS,
    TYPOLOGIES,
    APIClient,
    APIError,
    alert_rows,
    cited_ids,
    evidence_rows,
)
from tracegraph.config import settings

EXAMPLE_QUESTIONS = (
    "How much did account 10:8000588D0 send between 1 and 5 September 2022?",
    "Are there any typology alerts on account 19329:810E5B020?",
)
DISCLAIMER = (
    "Customer records and their links to watchlist or offshore records are synthetic. "
    "Alerts are pattern matches, not findings of wrongdoing. "
    "Every SAR is a draft that requires analyst review."
)

st.set_page_config(page_title="AML Investigation Copilot", layout="wide")


def client() -> APIClient:
    return APIClient(st.session_state.get("api_url", settings.ui_api_url),
                     settings.ui_request_timeout_seconds)  # fmt: skip


def show_status(status: str) -> None:
    label = STATUS_LABELS.get(status, status)
    if status in ("answered", "drafted"):
        st.success(label)
    elif status in ("ambiguous",):
        st.info(label)
    else:
        st.warning(label)


def show_evidence(result: dict[str, Any]) -> None:
    evidence = result.get("evidence") or []
    if not evidence:
        return
    cited = cited_ids(result.get("sentences") or [])
    with st.expander(f"Evidence ({len(evidence)} items, {len(cited)} cited)"):
        st.dataframe(evidence_rows(evidence, cited), hide_index=True, width="stretch")


def show_notices(result: dict[str, Any]) -> None:
    for notice in result.get("notices") or []:
        st.caption(f"ⓘ {notice}")


def call(action: str, fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
    with st.spinner(action):
        try:
            result: dict[str, Any] = fn(*args, **kwargs)
            return result
        except APIError as exc:
            st.error(str(exc))
            return None


# Sidebar: API connection.
with st.sidebar:
    st.header("Connection")
    st.session_state.setdefault("api_url", settings.ui_api_url)
    st.text_input("API URL", key="api_url")
    if st.button("Check health"):
        health = call("Checking…", client().health)
        if health:
            st.success(f"API {health['status']}: " + ", ".join(
                f"{name} {state}" for name, state in health["services"].items()))  # fmt: skip
    st.divider()
    st.caption(DISCLAIMER)

st.title("AML Investigation Copilot")
st.caption("Answers cite graph IDs; every cited ID is checked against the retrieved evidence.")

ask_tab, alerts_tab, sar_tab = st.tabs(["Ask", "Alerts", "SAR draft"])

with ask_tab:
    st.write("Ask about a customer, account, payment path, ICIJ entity or typology alert.")
    cols = st.columns(len(EXAMPLE_QUESTIONS))
    for col, example in zip(cols, EXAMPLE_QUESTIONS, strict=True):
        if col.button(example, width="stretch"):
            st.session_state["question"] = example
    with st.form("ask"):
        question = st.text_area("Question", key="question", max_chars=1000)
        submitted = st.form_submit_button("Ask", type="primary")
    if submitted and question.strip():
        result = call("Retrieving and drafting a cited answer…", client().ask, question.strip())
        if result:
            show_status(result["status"])
            sentences = result.get("sentences") or []
            if sentences:
                for sentence in sentences:
                    st.write(sentence["text"])
                    st.caption("Cites: " + ", ".join(sentence.get("citations", [])))
            else:
                st.write(result["answer"])
            show_notices(result)
            show_evidence(result)
            with st.expander("Route and trace"):
                st.json({"route": result.get("route"), "links": result.get("links"),
                         "trace": result.get("trace")})  # fmt: skip

with alerts_tab:
    st.write("Find typology alerts by account or customer ID, or browse one typology.")
    mode = st.radio("Look up by", ["Account or customer ID", "Typology"], horizontal=True)
    subject: str | None
    typology: str | None
    with st.form("alerts"):
        if mode == "Typology":
            typology = st.selectbox("Typology", TYPOLOGIES)
            subject = None
        else:
            subject = st.text_input("Account or customer ID", placeholder="19329:810E5B020")
            typology = None
        limit = st.slider("Maximum alerts", 5, 100, 20)
        searched = st.form_submit_button("Search", type="primary")
    if searched:
        if mode != "Typology" and not (subject or "").strip():
            st.warning("Enter an account or customer ID.")
        else:
            found = call("Looking up alerts…", client().alerts, subject, typology, limit)
            st.session_state["alerts_found"] = found
    found = st.session_state.get("alerts_found")
    if found is not None:
        rows = alert_rows(found["alerts"])
        if not rows:
            st.info("No typology alert flags this account or customer.")
        else:
            st.dataframe(rows, hide_index=True, width="stretch")
            show_notices(found)
            picked = st.selectbox("Draft a SAR for", [r["alert_id"] for r in rows])
            if st.button("Use this alert in the SAR tab"):
                st.session_state["sar_alert_id"] = picked
                st.success(f"{picked} is set in the SAR draft tab.")

with sar_tab:
    st.write(
        "Draft a SAR narrative for one alert. Every sentence cites the alert, accounts or "
        "payments it rests on; a draft that fails the grounding check is not shown. "
        "Drafting takes about 1–2 minutes on the local model."
    )
    with st.form("sar"):
        alert_id = st.text_input(
            "Alert ID", key="sar_alert_id", placeholder="alert-0016267745f71997"
        )
        drafted = st.form_submit_button("Draft SAR", type="primary")
    if drafted:
        if not ALERT_ID.match(alert_id.strip()):
            st.warning("An alert ID looks like alert- followed by 16 hex characters.")
        else:
            draft = call("Drafting a grounded SAR narrative…", client().sar, alert_id)
            if draft:
                show_status(draft["status"])
                st.code(draft["text"], language=None, wrap_lines=True)
                if draft["status"] == "drafted":
                    st.download_button(
                        "Download draft (.txt)",
                        draft["text"],
                        file_name=f"sar-draft-{draft['alert_id']}.txt",
                    )
                show_evidence(draft)
