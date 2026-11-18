"""Casework: one case per alert. Review the evidence and its graph, edit the SAR draft, and record
the disposition. The copilot drafts and shows evidence; only the analyst decides, and every action
is written to the case's audit trail."""

from typing import Any

import streamlit as st
from shared import (
    TYPOLOGY_COLORS,
    TYPOLOGY_INFO,
    analyst,
    call,
    client,
    key_values,
    money,
    notices,
    page_header,
    subgraph_dot,
)

from tracegraph.api.client import CASE_ID, CASE_STATUSES, DECISIONS

STATUS_COLORS = {"open": "blue", "escalated": "orange", "closed": "gray", "sar_filed": "red"}
ACTIONS = {"case_opened": "Case opened", "sar_draft_generated": "SAR draft generated",
           "sar_draft_saved": "SAR draft reviewed and saved",
           "disposition": "Disposition recorded"}  # fmt: skip

page_header(
    "Casework",
    "Cases",
    "One case per alert. The copilot gathers the evidence and drafts the SAR narrative; you "
    "review, edit and decide. Every action is recorded with your name, the time and your reason.",
)

# --- Case list ------------------------------------------------------------------------------
filters = st.columns([3, 2], vertical_alignment="bottom")
status = filters[0].segmented_control(
    "Status", ["All", *CASE_STATUSES], default="All", key="cases_status",
    format_func=lambda s: CASE_STATUSES.get(s, s),
)  # fmt: skip
listing = call(
    "Loading cases…", client().cases, None if status in (None, "All") else status, 0, 100
)
if listing is not None:
    rows = [
        {
            "Case": c["case_id"],
            "Status": CASE_STATUSES.get(c["status"], c["status"]),
            "Alert": c["alert_id"],
            "Opened by": c.get("opened_by"),
            "Last update": c.get("updated_at"),
            "SAR draft": "Reviewed" if c.get("sar_reviewed_by")
            else ("Unreviewed" if c.get("sar_draft") else "—"),
        }
        for c in listing["cases"]
    ]  # fmt: skip
    if not rows:
        st.info("No cases yet. Open one from an alert in the Alert queue.")
    else:
        event = st.dataframe(rows, hide_index=True, width="stretch", on_select="rerun",
                             selection_mode="single-row", key="cases_table")  # fmt: skip
        if event and event.selection.rows:
            st.session_state["case_id"] = rows[event.selection.rows[0]]["Case"]

case_id = st.session_state.get("case_id")
if not case_id or not CASE_ID.match(case_id):
    st.stop()

# --- Case view ------------------------------------------------------------------------------
st.divider()
view: dict[str, Any] | None = call("Loading case…", client().case, case_id)
if view is None:
    st.stop()
case = view["case"]
props = view["alert"]["props"]
final = case["status"] in ("closed", "sar_filed")

head = st.columns([5, 2], vertical_alignment="center")
with head[0]:
    st.subheader(f"Case {case_id}", anchor=False)
    # Both badges on one line (side-by-side columns truncate them in a narrow window).
    status_color = STATUS_COLORS.get(case["status"], "gray")
    typology = props.get("typology") or ""
    st.markdown(
        f":{status_color}-badge[{CASE_STATUSES[case['status']]}] "
        f":{TYPOLOGY_COLORS.get(typology, 'gray')}-badge[{typology}]"
    )
head[1].caption(f"Opened by {case.get('opened_by')} · {case.get('opened_at')}")

evidence_tab, draft_tab, decision_tab, audit_tab = st.tabs(
    ["Evidence", "SAR draft", "Disposition", "Audit trail"]
)

with evidence_tab:
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Alert subgraph**")
        st.caption("Flagged accounts are shaded; arrows are the alert's payments.")
        st.graphviz_chart(subgraph_dot(view["subgraph"]), width="stretch")
    with right:
        key_values(
            [
                ("Alert", view["alert"]["id"]),
                ("Summary", props.get("summary")),
                ("Typology", TYPOLOGY_INFO.get(props.get("typology", ""))),
                ("Window", f"{props.get('window_start')} to {props.get('window_end')}"),
                ("Detection thresholds", props.get("detection_thresholds")),
            ]
        )
    st.markdown("**Flagged accounts**")
    st.dataframe(
        [
            {"Account": a["id"], "Owner": a["props"].get("owner_name"),
             "Owner ID": a["props"].get("owner_customer_id"),
             "Country": a["props"].get("owner_country")}
            for a in view["accounts"]
        ],
        hide_index=True,
        width="stretch",
    )  # fmt: skip
    st.markdown("**Payments**")
    st.dataframe(
        [
            {"Transaction": p["id"], "Time": p["props"].get("timestamp"),
             "From": p["props"].get("from_account"), "To": p["props"].get("to_account"),
             "Amount": money(p["props"].get("amount_paid"), p["props"].get("payment_currency")),
             "Format": p["props"].get("payment_format")}
            for p in view["payments"]
        ],
        hide_index=True,
        width="stretch",
    )  # fmt: skip
    notices(view["notices"])

with draft_tab:
    if case.get("sar_reviewed_by"):
        st.success(f"Reviewed and saved by {case['sar_reviewed_by']} · {case['sar_reviewed_at']}")
    elif case.get("sar_draft"):
        st.warning("This draft was generated by the copilot and has not been reviewed. Read and "
                   "edit it, then save it to mark it reviewed.")  # fmt: skip
    if not final and st.button(
        "Generate draft from evidence",
        icon=":material/auto_awesome:",
        help="Drafting can take several minutes on the local model.",
    ):
        name = analyst()
        if name:
            result = call("Drafting a grounded SAR narrative…", client().generate_case_draft,
                          case_id, name)  # fmt: skip
            if result is None:
                st.info("If drafting timed out, the API may still finish and save the draft. "
                        "Reload this case in a few minutes before generating again.")  # fmt: skip
            elif result["draft"]["status"] != "drafted":
                st.error("The copilot could not produce a grounded draft (insufficient "
                         "evidence). Write the narrative yourself.")  # fmt: skip
            else:
                st.session_state.pop(f"draft_text_{case_id}", None)
                st.rerun()
    text = st.text_area(
        "Narrative",
        value=case.get("sar_draft") or "",
        height=320,
        key=f"draft_text_{case_id}",
        disabled=final,
        help="The 'DRAFT – requires analyst review' label is always kept.",
    )
    if not final and st.button("Save reviewed draft", type="primary", icon=":material/save:",
                               disabled=not text.strip()):  # fmt: skip
        name = analyst()
        if name and call("Saving draft…", client().save_case_draft, case_id, name, text):
            st.rerun()
    if case.get("sar_draft"):
        st.download_button("Download draft (.txt)", case["sar_draft"],
                           file_name=f"{case_id}-sar-draft.txt",
                           icon=":material/download:")  # fmt: skip

with decision_tab:
    if final:
        st.info(f"Final decision: **{DECISIONS[case['decision']]}** by {case['decided_by']} "
                f"on {case['decided_at']}.\n\nRationale: {case['rationale']}")  # fmt: skip
    else:
        allowed = ["close", "file_sar"] if case["status"] == "escalated" else list(DECISIONS)
        with st.form(f"disposition_{case_id}", border=False):
            decision = st.radio("Decision", allowed, format_func=DECISIONS.__getitem__)
            rationale = st.text_area("Rationale (required, at least 10 characters)", height=120)
            confirm = st.checkbox("I have reviewed the evidence and the SAR draft for this case.")
            submitted = st.form_submit_button("Record decision", type="primary")
        st.caption("Filing records your decision only; nothing is sent to a regulator. A SAR "
                   "can be filed only after its draft has been reviewed and saved.")  # fmt: skip
        if submitted:
            name = analyst()
            if not confirm:
                st.warning("Confirm that you have reviewed the evidence and the draft.")
            elif len(rationale.strip()) < 10:
                st.warning("Write a rationale of at least 10 characters.")
            elif decision == "file_sar" and not case.get("sar_reviewed_by"):
                st.warning("Review and save the SAR draft before filing.")
            elif name and call("Recording decision…", client().dispose, case_id, name, decision,
                               rationale.strip()):  # fmt: skip
                st.rerun()

with audit_tab:
    st.caption("Append-only: every action on this case, in order.")
    st.dataframe(
        [
            {"#": e.get("seq"), "Time (UTC)": e["at"], "Analyst": e["analyst"],
             "Action": ACTIONS.get(e["action"], e["action"]),
             "Status": f"{e.get('from_status') or '—'} → {e.get('to_status')}",
             "Rationale": e.get("rationale"), "Details": e.get("details")}
            for e in view["audit"]
        ],
        hide_index=True,
        width="stretch",
    )  # fmt: skip
