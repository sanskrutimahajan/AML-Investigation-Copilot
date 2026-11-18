"""SAR drafting: review an alert's evidence, then draft a grounded narrative for analyst review."""

from typing import Any

import streamlit as st
from shared import call, client, key_values, notices, page_header, typology_badge

from tracegraph.api.client import ALERT_ID, STATUS_LABELS, cited_ids, evidence_rows

page_header(
    "Reporting",
    "SAR drafting",
    "Drafts a suspicious activity report narrative from one alert. Every sentence cites the "
    "alert, accounts or payments it rests on; a draft that fails the grounding check is not "
    "shown. Drafting takes one to two minutes on the local model.",
)

with st.form("sar_form", border=False):
    row = st.columns([4, 1, 1], vertical_alignment="bottom")
    alert_id = (
        row[0]
        .text_input("Alert ID", key="sar_alert_id", placeholder="alert-0016267745f71997")
        .strip()
    )
    load = row[1].form_submit_button("Load alert", width="stretch")
    draft_now = row[2].form_submit_button("Draft SAR", type="primary", width="stretch")

if (load or draft_now) and not ALERT_ID.match(alert_id):
    st.warning("An alert ID looks like alert- followed by 16 hex characters.")
elif load or draft_now:
    st.session_state["sar_loaded"] = call("Loading alert…", client().alert_detail, alert_id)
    st.session_state["sar_draft"] = None
    if draft_now and st.session_state["sar_loaded"] is not None:
        st.session_state["sar_draft"] = call(
            "Drafting a grounded SAR narrative…", client().sar, alert_id
        )

loaded: dict[str, Any] | None = st.session_state.get("sar_loaded")
draft: dict[str, Any] | None = st.session_state.get("sar_draft")
if loaded:
    props = loaded["alert"]["props"]
    left, right = st.columns([2, 3], gap="large")
    with left:
        with st.container(border=True):
            st.markdown(f"**{loaded['alert']['id']}**")
            typology_badge(props.get("typology"))
            key_values(
                [
                    ("Summary", props.get("summary")),
                    ("Window", f"{props.get('window_start')} to {props.get('window_end')}"),
                    ("Flagged accounts", len(loaded["accounts"])),
                    ("Payments", len(loaded["payments"])),
                    ("Thresholds", props.get("detection_thresholds")),
                ]
            )
    with right:
        if not draft:
            st.info("Review the alert, then choose Draft SAR to generate a narrative.")
        else:
            status = draft["status"]
            if status != "drafted":
                st.warning(STATUS_LABELS.get(status, status))
            st.html('<div class="wb-draft-label">Draft – requires analyst review</div>')
            st.code(draft["text"], language=None, wrap_lines=True)
            if status == "drafted":
                st.download_button(
                    "Download draft (.txt)",
                    draft["text"],
                    file_name=f"sar-draft-{draft['alert_id']}.txt",
                    icon=":material/download:",
                )
            evidence = draft.get("evidence") or []
            if evidence:
                cited = cited_ids(draft.get("sentences") or [])
                with st.expander(f"Evidence ({len(evidence)} records, {len(cited)} cited)"):
                    st.dataframe(evidence_rows(evidence, cited), hide_index=True,
                                 width="stretch")  # fmt: skip
            notices(draft.get("notices"))
    notices(loaded["notices"])
