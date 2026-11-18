"""Shared pieces of the investigator workbench: API access, page chrome and formatting.

Every page calls the API only (ADR 011); nothing here opens a database session or calls the LLM.
"""

from collections.abc import Callable
from datetime import date
from typing import Any

import streamlit as st

from tracegraph.api.client import ANALYST, APIClient, APIError
from tracegraph.config import settings

# What each laundering pattern ("typology") is, and why it matters, in plain words.
TYPOLOGY_INFO = {
    "FAN-IN": "Many different accounts pay into one account in a short time. Can mean money from "
    "many sources being pooled, for example collected through money mules.",
    "FAN-OUT": "One account pays many different accounts in a short time. Can mean money being "
    "split up and spread out to make it harder to follow.",
    "CYCLE": "Money leaves an account and comes back to it through other accounts. This "
    "round-tripping can make dirty money look like legitimate income.",
    "SCATTER-GATHER": "One account splits money across several middle accounts, which all pay "
    "the same final account. Hides that the money really went from one party to another.",
    "LAYERING": "Money is passed along a chain of accounts, each paying it on soon after "
    "receiving it. Every hop puts more distance between the money and where it came from.",
    "MULE-HUB": "One account collects from several senders and then pays the money out. "
    "Typical of a money-mule account used as a pass-through.",
    "STRUCTURING": "Several cash deposits into one account, each just under the amount banks "
    "must report (10,000 USD in the US). Splitting cash this way avoids the report.",
}
TYPOLOGY_COLORS = {"FAN-IN": "blue", "FAN-OUT": "violet", "CYCLE": "orange",
                   "SCATTER-GATHER": "red", "LAYERING": "green", "MULE-HUB": "primary",
                   "STRUCTURING": "gray"}  # fmt: skip
SOURCE_LABELS = {"opensanctions": "Sanctions list (OpenSanctions)",
                 "icij": "Offshore Leaks (ICIJ)"}  # fmt: skip
DISCLAIMER = (
    "Customer records and their links to watchlist or offshore records are synthetic. Alerts "
    "are pattern matches, not findings of wrongdoing. SAR narratives are drafts that require "
    "analyst review."
)

_CSS = """
<style>
  .block-container { padding-top: 2.2rem; max-width: 1280px; }
  h1 { font-size: 1.55rem !important; letter-spacing: -0.01em; margin-bottom: 0 !important; }
  h2 { font-size: 1.15rem !important; }
  h3 { font-size: 1.0rem !important; }
  .wb-eyebrow { font-size: 0.72rem; font-weight: 600; letter-spacing: 0.08em;
                text-transform: uppercase; color: #5B6B7F; margin-bottom: 0.15rem; }
  .wb-subtitle { color: #5B6B7F; font-size: 0.9rem; margin: 0.25rem 0 0.4rem 0; }
  /* Product name above the page menu (Streamlit always renders the menu first). */
  [data-testid="stSidebarNav"]::before {
    content: "AML Investigation Copilot\\A Investigator workbench";
    white-space: pre; display: block; text-align: left; padding: 0.2rem 1rem 1.1rem 1rem;
    font-weight: 600; font-size: 1.0rem; line-height: 1.5; letter-spacing: 0.01em;
    border-bottom: 1px solid #2A4A6B; margin-bottom: 0.8rem;
  }
  [data-testid="stSidebarNav"] [data-testid="stNavSectionHeader"] {
    font-size: 0.7rem; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.7;
  }
  .wb-notice { font-size: 0.78rem; color: #5B6B7F; border-left: 3px solid #D6DCE4;
               padding: 0.15rem 0 0.15rem 0.6rem; margin: 0.2rem 0; }
  /* A grid keeps each value beside its label, even when a long label wraps. */
  .wb-kv { font-size: 0.85rem; line-height: 1.45; max-width: 760px; display: grid;
           grid-template-columns: minmax(8rem, 34%) 1fr; column-gap: 1rem; row-gap: 0.35rem; }
  .wb-kv dt { color: #5B6B7F; margin: 0; }
  .wb-kv dd { margin: 0; font-weight: 500; overflow-wrap: anywhere; }
  .wb-draft-label { background: #FEF3F2; color: #B42318; border: 1px solid #FECDCA;
                    font-size: 0.78rem; font-weight: 600; letter-spacing: 0.04em;
                    text-transform: uppercase; padding: 0.3rem 0.6rem; border-radius: 3px;
                    display: inline-block; margin-bottom: 0.6rem; }
  [data-testid="stMetricValue"] { font-size: 1.6rem; }
  [data-testid="stMetricLabel"] p { font-size: 0.78rem; text-transform: uppercase;
                                    letter-spacing: 0.05em; color: #5B6B7F; }
  [data-testid="stSidebar"] .wb-footer { font-size: 0.72rem; opacity: 0.65; line-height: 1.4; }
</style>
"""


def apply_style() -> None:
    st.html(_CSS)


def client() -> APIClient:
    url = st.session_state.get("api_url") or settings.ui_api_url
    return APIClient(
        url, settings.ui_request_timeout_seconds, draft_timeout=settings.ui_draft_timeout_seconds
    )


def call(action: str, fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
    """Run one API call with a spinner; show a readable error and return None on failure."""
    with st.spinner(action):
        try:
            return fn(*args, **kwargs)
        except APIError as exc:
            st.error(str(exc))
            return None


def page_header(eyebrow: str, title: str, subtitle: str) -> None:
    st.html(f'<div class="wb-eyebrow">{eyebrow}</div>')
    st.title(title, anchor=False)
    st.html(f'<div class="wb-subtitle">{subtitle}</div>')


def notices(items: list[str] | None) -> None:
    for text in items or []:
        st.html(f'<div class="wb-notice">{_escape(text)}</div>')


def key_values(pairs: list[tuple[str, Any]]) -> None:
    """A two-column definition list; empty values are shown as an em dash."""
    rows = "".join(f"<dt>{_escape(k)}</dt><dd>{_escape(_display(v))}</dd>" for k, v in pairs)
    st.html(f'<dl class="wb-kv">{rows}</dl>')


def typology_badge(typology: str | None) -> None:
    if typology:
        st.badge(typology, color=TYPOLOGY_COLORS.get(typology, "gray"))  # type: ignore[arg-type]


def go_to(page: str, **state: Any) -> None:
    """Hand values to another page through session state, then switch to it."""
    for key, value in state.items():
        st.session_state[key] = value
    st.switch_page(page)


BAND_COLORS = {"High": "red", "Medium": "orange", "Low": "gray"}


def scenario_label(typology: str | None) -> str:
    """`TM-01 Funnel account activity`, as analysts see scenarios (ADR 017)."""
    from tracegraph.detection.scenarios import label

    return label(typology)


def scenario_steps(scenario: dict[str, Any] | None) -> None:
    """The scenario's suggested investigation steps."""
    steps = (scenario or {}).get("next_steps") or []
    if steps:
        st.markdown("**Suggested checks**")
        st.markdown("\n".join(f"- {step}" for step in steps))


def role() -> str:
    """The role chosen in the sidebar (self-declared; no login in this prototype)."""
    return str(st.session_state.get("role") or "analyst")


def as_of() -> date | None:
    """The replay date from the sidebar, or None for all alerts."""
    value = st.session_state.get("as_of")
    return value if isinstance(value, date) else None


def replay_notice() -> None:
    if as_of():
        st.info(f"Replay: showing alerts raised by the end of {as_of():%Y-%m-%d}. Clear the date "
                "in the sidebar to see all alerts.", icon=":material/history:")  # fmt: skip


def analyst() -> str | None:
    """The self-declared analyst name from the sidebar, or None (with a warning) if unset or
    invalid. There is no authentication in this prototype."""
    name = (st.session_state.get("analyst") or "").strip()
    if ANALYST.match(name):
        return name
    st.warning(
        "Enter your analyst name in the sidebar first; every case action is logged under it."
    )
    return None


def alert_facts(facts: dict[str, Any] | None) -> None:
    """The alert's payments re-read from the graph by a fixed query, as a check on the detector."""
    if not facts:
        return
    st.markdown("**Re-checked in the graph**")
    st.caption("Recomputed from the alert's payments by a read-only query, not by the detector.")
    key_values(
        [
            ("Payments", facts.get("payments")),
            ("Distinct senders into flagged accounts", facts.get("distinct_senders_into_flagged")),
            ("Distinct receivers from flagged accounts",
             facts.get("distinct_receivers_from_flagged")),
            ("Payments out of flagged accounts", facts.get("payments_out_of_flagged")),
            ("First / last payment", f"{facts.get('first_payment')} / {facts.get('last_payment')}"),
            ("Smallest / largest payment",
             f"{money(facts.get('smallest_amount'))} / {money(facts.get('largest_amount'))}"),
            ("Formats", facts.get("formats")),
            ("Currencies", facts.get("currencies")),
        ]
    )  # fmt: skip


def subgraph_dot(graph: dict[str, Any]) -> str:
    """Graphviz DOT for an alert's subgraph: flagged accounts filled, payments as labeled edges."""
    lines = [
        "digraph alert {",
        '  rankdir=LR; bgcolor="transparent";',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10,'
        ' color="#5B6B7F", fillcolor="#FFFFFF"];',
        '  edge [fontname="Helvetica", fontsize=9, color="#5B6B7F"];',
    ]
    for node in graph.get("nodes", []):
        # The DOT line break (backslash-n) is added after escaping, so it survives.
        label = "\\n".join(_dot(part) for part in (node["id"], node.get("owner")) if part)
        fill = ', fillcolor="#DCE7F3", color="#1F4E79", penwidth=1.6' if node.get("flagged") else ""
        lines.append(f'  "{_dot(node["id"])}" [label="{label}"{fill}];')
    for edge in graph.get("edges", []):
        amount = money(edge.get("amount"), edge.get("currency"))
        lines.append(f'  "{_dot(edge["source"])}" -> "{_dot(edge["target"])}" '
                     f'[label="{_dot(amount)}", tooltip="{_dot(edge["id"])}"];')  # fmt: skip
    lines.append("}")
    return "\n".join(lines)


def _dot(text: Any) -> str:
    return str(text).replace("\\", "").replace('"', "'")


def money(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.2f}"
    return f"{text} {currency}" if currency else text


def _display(value: Any) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def _escape(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
