"""Investigator workbench (Streamlit): alert triage, casework (evidence graph, reviewed SAR draft,
disposition, audit trail), subject profiles, name screening, a cited investigation assistant and
SAR drafting.

Run with `make ui` while the API is up (`make api`). Every page calls the API only (ADR 011);
answers and drafts have passed the API's grounding check.
"""

import streamlit as st
from shared import DISCLAIMER, apply_style, call, client

from tracegraph.config import settings

st.set_page_config(
    page_title="AML Investigation Workbench",
    page_icon=":material/account_balance:",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_style()

PAGES = {
    "Monitoring": [
        st.Page("views/overview.py", title="Overview", icon=":material/space_dashboard:",
                default=True),
        st.Page("views/alert_queue.py", title="Alert queue", icon=":material/inbox:"),
    ],
    "Casework": [
        st.Page("views/cases.py", title="Cases", icon=":material/folder_open:"),
    ],
    "Investigation": [
        st.Page("views/subject.py", title="Subject profile", icon=":material/badge:"),
        st.Page("views/screening.py", title="Name screening", icon=":material/policy:"),
        st.Page("views/assistant.py", title="Investigation assistant",
                icon=":material/manage_search:"),
    ],
    "Reporting": [
        st.Page("views/sar.py", title="SAR drafting", icon=":material/description:"),
    ],
}  # fmt: skip

page = st.navigation(PAGES, position="sidebar", expanded=True)

with st.sidebar:
    st.divider()
    st.text_input(
        "Analyst name",
        key="analyst",
        placeholder="e.g. Dana Analyst",
        help="Recorded on every case action. This prototype has no login.",
    )
    with st.expander("Connection", icon=":material/settings_ethernet:"):
        st.session_state.setdefault("api_url", settings.ui_api_url)
        st.text_input("API URL", key="api_url")
        if st.button("Check status", width="stretch"):
            health = call("Checking services…", client().health)
            if health:
                st.success(
                    "All services healthy"
                    if health["status"] == "healthy"
                    else f"Status: {health['status']}"
                )
    st.html(f'<div class="wb-footer">{DISCLAIMER}</div>')

page.run()
