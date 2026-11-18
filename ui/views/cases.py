"""Casework: one case per customer, as in a bank's investigation unit. Work lists, the customer's
alerts with their evidence and graph, the SAR draft, and role-based decisions: an analyst (L1)
escalates or recommends, an investigator (L2) recommends, and an approver who did not make the
recommendation approves or returns it. Every action is in the audit trail."""

from datetime import UTC, datetime
from typing import Any

import streamlit as st
from shared import (
    BAND_COLORS,
    alert_facts,
    analyst,
    call,
    client,
    key_values,
    money,
    notices,
    page_header,
    role,
    scenario_steps,
    subgraph_dot,
)

from tracegraph.api.client import CASE_ID, CASE_STATUSES, RECOMMENDATIONS, ROLES

STATUS_COLORS = {"open": "blue", "escalated": "violet", "pending_approval": "orange",
                 "closed": "gray", "sar_filed": "red"}  # fmt: skip
ACTIONS = {"case_opened": "Case opened", "alerts_added": "Alerts added", "assign": "Assigned",
           "escalate": "Escalated to L2", "recommend": "Recommendation submitted",
           "approve": "Approved", "return": "Returned for more work",
           "edit_draft": "SAR draft changed", "migrated": "Migrated to customer case",
           "sar_draft_generated": "SAR draft generated", "sar_draft_saved": "SAR draft saved",
           "disposition": "Disposition (old model)"}  # fmt: skip
VIEWS = {"mine": "My cases", "approval": "Pending approval", "open": "All open", "all": "All"}


def due_text(iso: str | None) -> str:
    """'in 6 days', 'today' or '3 days overdue'."""
    if not iso:
        return "—"
    days = (datetime.fromisoformat(iso) - datetime.now(UTC)).days
    return "today" if days == 0 else f"in {days} days" if days > 0 else f"{-days} days overdue"


page_header(
    "Casework",
    "Cases",
    "One case per customer, with all their alerts. Investigate, draft the SAR, and send a "
    "recommendation for approval. The copilot drafts; people decide.",
)

# --- Work lists ------------------------------------------------------------------------------
view = st.segmented_control("View", list(VIEWS), format_func=VIEWS.__getitem__, default="mine",
                            key="cases_view")  # fmt: skip
me = (st.session_state.get("analyst") or "").strip()
if view == "mine" and not me:
    st.info("Enter your analyst name in the sidebar to see the cases assigned to you.")
    listing = None
elif view == "mine":
    listing = call("Loading cases…", client().cases, None, me, 0, 100)
elif view == "approval":
    listing = call("Loading cases…", client().cases, "pending_approval", None, 0, 100)
else:
    listing = call("Loading cases…", client().cases, None, None, 0, 100)
cases: list[dict[str, Any]] = (listing or {}).get("cases", [])
if view == "open":
    cases = [c for c in cases if c["status"] not in ("closed", "sar_filed")]

if listing is not None and not cases:
    st.info("No cases in this view. Open one from an alert in the Alert queue.")
elif cases:
    rows = [
        {
            "Customer": c.get("subject_name") or "—",
            "Priority": f"{c.get('priority_band') or '—'} ({c.get('priority') or '—'})",
            "Status": CASE_STATUSES.get(c["status"], c["status"]),
            "Owner": c.get("assigned_to") or "—",
            "Review due": due_text(c.get("due_at"))
            if c["status"] not in ("closed", "sar_filed") else "—",
            "SAR deadline": due_text(c.get("sar_due_at"))
            if c["status"] not in ("closed", "sar_filed") else "—",
            "Alerts": c.get("alerts"),
            "Case": c["case_id"],
        }
        for c in cases
    ]  # fmt: skip
    event = st.dataframe(rows, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="cases_table")  # fmt: skip
    if event and event.selection.rows:
        st.session_state["case_id"] = rows[event.selection.rows[0]]["Case"]
        st.session_state.pop("case_alert_id", None)

case_id = st.session_state.get("case_id")
if not case_id or not CASE_ID.match(case_id):
    st.stop()

# --- Case view -------------------------------------------------------------------------------
st.divider()
view_data: dict[str, Any] | None = call("Loading case…", client().case, case_id,
                                         st.session_state.get("case_alert_id"))  # fmt: skip
if view_data is None:
    st.stop()
case = view_data["case"]
status = case["status"]
final = status in ("closed", "sar_filed")
props = view_data["alert"]["props"]
scenario = view_data["alert"].get("scenario") or {}

head = st.columns([5, 3], vertical_alignment="top")
with head[0]:
    st.subheader(f"{case.get('subject_name') or '—'}", anchor=False)
    st.markdown(
        f":{STATUS_COLORS.get(status, 'gray')}-badge[{CASE_STATUSES.get(status, status)}] "
        f":{BAND_COLORS.get(case.get('priority_band'), 'gray')}-badge["
        f"{case.get('priority_band') or '—'} priority · {case.get('priority') or '—'}] "
        f"&nbsp; `{case.get('subject_customer_id')}` · `{case_id}`"
    )
with head[1]:
    key_values(
        [
            ("Owner", case.get("assigned_to")),
            ("Review due", "—" if final else due_text(case.get("due_at"))),
            ("SAR deadline", "—" if final else due_text(case.get("sar_due_at"))),
        ]
    )
if not final:
    with st.expander("Reassign", icon=":material/person:"):
        cols = st.columns([3, 1], vertical_alignment="bottom")
        assignee = cols[0].text_input("Assign to", placeholder="e.g. Sam Investigator")
        if cols[1].button("Assign", width="stretch", disabled=not assignee.strip()):
            name = analyst()
            if name and call("Assigning…", client().assign, case_id, assignee.strip(), name,
                             role()):  # fmt: skip
                st.rerun()

evidence_tab, draft_tab, decision_tab, audit_tab = st.tabs(
    ["Alerts and evidence", "SAR draft", "Decision", "Audit trail"]
)

with evidence_tab:
    options = {a["alert_id"]: a for a in view_data["case_alerts"]}
    if len(options) > 1:
        # Seed the widget's state once, so the selection survives reruns without a warning.
        st.session_state.setdefault("case_alert_id", view_data["selected_alert_id"])
        st.selectbox(
            f"Alert ({len(options)} on this customer)",
            list(options),
            format_func=lambda aid: (
                f"{options[aid]['scenario']} · priority {options[aid]['priority']} · {aid}"
            ),
            key="case_alert_id",
        )
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown(f"**{scenario.get('scenario_id', '')} {scenario.get('name', '')}**")
        st.caption("Flagged accounts are shaded; arrows are the alert's payments.")
        st.graphviz_chart(subgraph_dot(view_data["subgraph"]), width="stretch")
    with right:
        key_values(
            [
                ("What was found", props.get("summary")),
                ("Why it is a red flag", scenario.get("red_flag")),
                ("Window", f"{props.get('window_start')} to {props.get('window_end')}"),
                ("Priority", props.get("priority_breakdown")),
            ]
        )
        scenario_steps(scenario)
        alert_facts(view_data.get("facts"))
    st.markdown("**Payments**")
    st.dataframe(
        [
            {"Transaction": p["id"], "Time": p["props"].get("timestamp"),
             "From": p["props"].get("from_account"), "To": p["props"].get("to_account"),
             "Amount": money(p["props"].get("amount_paid"), p["props"].get("payment_currency")),
             "Format": p["props"].get("payment_format")}
            for p in view_data["payments"]
        ],
        hide_index=True,
        width="stretch",
    )  # fmt: skip
    notices(view_data["notices"])

with draft_tab:
    editable = status in ("open", "escalated")
    if case.get("sar_reviewed_by"):
        st.success(f"Reviewed and saved by {case['sar_reviewed_by']} · {case['sar_reviewed_at']}")
    elif case.get("sar_draft"):
        st.warning("Generated by the copilot and not yet reviewed. Read and edit it, then save it "
                   "to mark it reviewed.")  # fmt: skip
    if not editable and not final:
        st.info("The draft is frozen while the recommendation waits for approval.")
    if editable and st.button(
        "Generate draft from the selected alert",
        icon=":material/auto_awesome:",
        help="Drafting can take several minutes on the local model.",
    ):
        name = analyst()
        if name:
            result = call("Drafting a grounded SAR narrative…", client().generate_case_draft,
                          case_id, name, role(), view_data["selected_alert_id"])  # fmt: skip
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
        disabled=not editable,
        help="The 'DRAFT – requires analyst review' label is always kept.",
    )
    if editable and st.button("Save reviewed draft", type="primary", icon=":material/save:",
                              disabled=not text.strip()):  # fmt: skip
        name = analyst()
        if name and call("Saving draft…", client().save_case_draft, case_id, name, role(), text):
            st.rerun()
    if case.get("sar_draft"):
        st.download_button("Download draft (.txt)", case["sar_draft"],
                           file_name=f"{case_id}-sar-draft.txt",
                           icon=":material/download:")  # fmt: skip


def rationale_form(key: str, label: str, submit: str) -> str | None:
    """A rationale box with a review confirmation; returns the rationale once submitted."""
    with st.form(key, border=False):
        text = st.text_area(label, height=110)
        confirm = st.checkbox("I have reviewed the evidence and the SAR draft for this case.")
        sent = st.form_submit_button(submit, type="primary")
    if not sent:
        return None
    if not confirm:
        st.warning("Confirm that you have reviewed the evidence and the draft.")
    elif len(text.strip()) < 10:
        st.warning("Write a rationale of at least 10 characters.")
    else:
        return text.strip()
    return None


with decision_tab:
    who = role()
    st.caption(f"You are acting as **{ROLES[who]}**. Filing records the decision only; nothing "
               "is sent to a regulator.")  # fmt: skip
    if final:
        st.info(f"Final decision: **{RECOMMENDATIONS.get(case.get('decision'), '—')}**, "
                f"recommended by {case.get('recommended_by') or '—'} and approved by "
                f"{case.get('decided_by')} on {case.get('decided_at')}.\n\n"
                f"Approver's rationale: {case.get('rationale')}")  # fmt: skip
    elif status == "pending_approval":
        st.markdown(
            f"**Recommendation:** {RECOMMENDATIONS[case['recommendation']]} — by "
            f"{case['recommended_by']} ({ROLES.get(case['recommended_role'], '')})\n\n"
            f"> {case.get('recommendation_rationale')}"
        )
        me = (st.session_state.get("analyst") or "").strip()
        if who != "approver":
            st.info("Waiting for an approver.")
        elif me == case["recommended_by"]:
            st.warning("Four eyes: you made this recommendation, so someone else must approve it.")
        else:
            choice = st.radio(
                "Approver decision",
                ["approve", "return"],
                format_func={
                    "approve": "Approve the recommendation",
                    "return": "Return for more work",
                }.__getitem__,
            )
            note = rationale_form(f"approval_{case_id}", "Approver's rationale (required)",
                                  "Record decision")  # fmt: skip
            name = analyst() if note else None
            if note and name and call("Recording decision…", client().decide, case_id, name,
                                      who, choice == "approve", note):  # fmt: skip
                st.rerun()
    elif who == "approver":
        st.info("Approvers act once an analyst or investigator submits a recommendation.")
    elif status == "escalated" and who != "investigator":
        st.info("This case was escalated: an investigator (L2) recommends next.")
    else:
        actions = ["recommend"] + (["escalate"] if status == "open" and who == "analyst" else [])
        action = st.radio(
            "Action",
            actions,
            horizontal=True,
            format_func={
                "recommend": "Submit a recommendation",
                "escalate": "Escalate to an investigator (L2)",
            }.__getitem__,
        )
        if action == "escalate":
            note = rationale_form(f"escalate_{case_id}", "Why escalate? (required)", "Escalate")
            name = analyst() if note else None
            if note and name and call("Escalating…", client().escalate, case_id, name, who,
                                      note):  # fmt: skip
                st.rerun()
        else:
            pick = st.radio(
                "Recommendation", list(RECOMMENDATIONS), format_func=RECOMMENDATIONS.__getitem__
            )
            if pick == "file_sar" and not case.get("sar_reviewed_by"):
                st.warning("Review and save the SAR draft before recommending a filing.")
            note = rationale_form(f"recommend_{case_id}", "Rationale (required)",
                                  "Submit for approval")  # fmt: skip
            name = analyst() if note else None
            if note and name and call("Submitting…", client().recommend, case_id, name, who,
                                      pick, note):  # fmt: skip
                st.rerun()

with audit_tab:
    st.caption("Append-only: every action on this case, in order.")
    st.dataframe(
        [
            {"#": e.get("seq"), "Time (UTC)": e["at"], "Person": e["analyst"],
             "Role": ROLES.get(e.get("role") or "", e.get("role") or "—"),
             "Action": ACTIONS.get(e["action"], e["action"]),
             "Status": f"{e.get('from_status') or '—'} → {e.get('to_status')}",
             "Rationale": e.get("rationale"), "Details": e.get("details")}
            for e in view_data["audit"]
        ],
        hide_index=True,
        width="stretch",
    )  # fmt: skip
