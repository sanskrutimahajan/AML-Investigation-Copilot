"""Alert queue: work alerts in priority order, review one, and open the customer's case."""

from typing import Any

import streamlit as st
from shared import (
    BAND_COLORS,
    TYPOLOGY_INFO,
    alert_facts,
    analyst,
    as_of,
    call,
    client,
    go_to,
    key_values,
    money,
    notices,
    page_header,
    replay_notice,
    role,
    scenario_label,
    scenario_steps,
)

from tracegraph.api.client import ALERT_ID, SUBJECT_ID

PAGE_SIZE = 25

page_header(
    "Monitoring",
    "Alert queue",
    "Highest priority first. Filter by scenario or priority band, or look up one customer or "
    "account. Open a case to start the investigation.",
)
replay_notice()

st.session_state.setdefault("queue_offset", 0)
filters = st.columns([3, 2, 2, 1], vertical_alignment="bottom")
with filters[0]:
    typology = st.selectbox(
        "Scenario",
        ["All", *TYPOLOGY_INFO],
        format_func=lambda t: "All scenarios" if t == "All" else scenario_label(t),
        key="queue_typology",
        on_change=lambda: st.session_state.update(queue_offset=0),
    )
with filters[1]:
    band = st.segmented_control(
        "Priority",
        ["All", "High", "Medium", "Low"],
        default="All",
        key="queue_band",
        on_change=lambda: st.session_state.update(queue_offset=0),
    )
with filters[2]:
    subject = st.text_input(
        "Customer or account ID", placeholder="e.g. 19329:810E5B020", key="queue_subject"
    ).strip()
with filters[3]:
    if st.button("Reset", width="stretch"):
        for key in ("queue_typology", "queue_band", "queue_subject", "queue_alert_id"):
            st.session_state.pop(key, None)
        st.session_state["queue_offset"] = 0
        st.rerun()

chosen = None if typology in (None, "All") else typology
if subject and not SUBJECT_ID.match(subject):
    st.warning("A customer ID looks like cust-8005B3110; an account ID like 19329:810E5B020.")
    result = None
elif subject:
    result = call("Looking up alerts…", client().alerts, subject, None, 100)
else:
    offset = st.session_state["queue_offset"]
    chosen_band = None if band in (None, "All") else band
    result = call("Loading alerts…", client().alert_queue, chosen, offset, PAGE_SIZE, as_of(),
                  chosen_band)  # fmt: skip

alerts: list[dict[str, Any]] = result["alerts"] if result else []
if subject and chosen:
    alerts = [a for a in alerts if a["props"].get("typology") == chosen]

if result is not None:
    if not alerts:
        st.info("No alerts match these filters.")
    else:
        table = [
            {
                "Priority": f"{a['props'].get('priority_band', '—')} "
                f"({a['props'].get('priority', '—')})",
                "Scenario": scenario_label(a["props"].get("typology")),
                "Customer": ", ".join(a["props"].get("owner_names") or []) or "—",
                "Window end": a["props"].get("window_end"),
                "Summary": a["props"].get("summary"),
                "Alert ID": a["id"],
            }
            for a in alerts
        ]
        event = st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key="queue_table",
            column_config={
                "Priority": st.column_config.TextColumn(width=110),
                "Scenario": st.column_config.TextColumn(width=300),
                "Customer": st.column_config.TextColumn(width=240),
                "Summary": st.column_config.TextColumn(width="large"),
            },
        )
        picked = event.selection.rows if event else []
        if picked:
            st.session_state["queue_alert_id"] = table[picked[0]]["Alert ID"]

        if not subject:
            offset = st.session_state["queue_offset"]
            pager = st.columns([1, 1, 6], vertical_alignment="center")
            if pager[0].button("Previous", disabled=offset == 0, width="stretch"):
                st.session_state["queue_offset"] = max(0, offset - PAGE_SIZE)
                st.rerun()
            if pager[1].button("Next", disabled=len(alerts) < PAGE_SIZE, width="stretch"):
                st.session_state["queue_offset"] = offset + PAGE_SIZE
                st.rerun()
            pager[2].caption(f"Showing alerts {offset + 1}–{offset + len(alerts)}")

alert_id = st.session_state.get("queue_alert_id")
if alert_id and ALERT_ID.match(alert_id):
    st.divider()
    detail = call("Loading alert…", client().alert_detail, alert_id)
    if detail is not None:
        props = detail["alert"]["props"]
        head = st.columns([3, 1, 1, 1], vertical_alignment="center")
        with head[0]:
            st.subheader(scenario_label(props.get("typology")), anchor=False)
            st.markdown(
                f":{BAND_COLORS.get(props.get('priority_band'), 'gray')}-badge["
                f"{props.get('priority_band', '—')} priority · {props.get('priority', '—')}] "
                f"&nbsp; `{alert_id}`"
            )
        accounts = detail["accounts"]
        payments = detail["payments"]
        if head[1].button("Open case", type="primary", width="stretch",
                          icon=":material/folder_open:"):  # fmt: skip
            name, who = analyst(), role()
            opened = name and call("Opening case…", client().open_case, alert_id, name, who)
            if opened:
                go_to("views/cases.py", case_id=opened["case"]["case_id"])
        if head[3].button("Draft SAR", width="stretch", icon=":material/description:"):
            go_to("views/sar.py", sar_alert_id=alert_id)
        owner_ids = sorted({a["props"].get("owner_customer_id") for a in accounts} - {None})
        if head[2].button("Open subject", width="stretch", disabled=not owner_ids,
                          icon=":material/badge:"):  # fmt: skip
            go_to("views/subject.py", subject_query=owner_ids[0], subject_id=None)

        info, totals = st.columns([3, 2], gap="large")
        with info:
            scenario = detail["alert"].get("scenario") or {}
            key_values(
                [
                    ("What was found", props.get("summary")),
                    ("Scenario", scenario.get("description")),
                    ("Why it is a red flag", scenario.get("red_flag")),
                    ("Window", f"{props.get('window_start')} to {props.get('window_end')}"),
                    ("Thresholds", props.get("detection_thresholds")),
                    ("Priority", props.get("priority_breakdown")),
                ]
            )
            scenario_steps(scenario)
            alert_facts(detail.get("facts"))
        with totals:
            by_currency: dict[str, float] = {}
            for p in payments:
                currency = p["props"].get("payment_currency") or "—"
                by_currency[currency] = by_currency.get(currency, 0.0) + float(
                    p["props"].get("amount_paid") or 0
                )
            metric_cols = st.columns(2)
            metric_cols[0].metric("Flagged accounts", len(accounts))
            metric_cols[1].metric("Payments", len(payments))
            for currency, amount in by_currency.items():
                st.caption(f"Total paid: {money(amount, currency)}")

        st.markdown("**Flagged accounts**")
        st.dataframe(
            [
                {
                    "Account": a["id"],
                    "Bank ID": a["props"].get("bank_id"),
                    "Owner": a["props"].get("owner_name"),
                    "Owner ID": a["props"].get("owner_customer_id"),
                    "Owner type": a["props"].get("owner_type"),
                    "Country": a["props"].get("owner_country"),
                }
                for a in accounts
            ],
            hide_index=True,
            width="stretch",
        )
        if len(owner_ids) > 1:
            pick = st.columns([3, 1], vertical_alignment="bottom")
            owner = pick[0].selectbox("Open another owner's profile", owner_ids)
            if pick[1].button("Open profile", width="stretch"):
                go_to("views/subject.py", subject_query=owner, subject_id=None)

        st.markdown("**Payments in the alert**")
        st.dataframe(
            [
                {
                    "Transaction": p["id"],
                    "Time": p["props"].get("timestamp"),
                    "From account": p["props"].get("from_account"),
                    "To account": p["props"].get("to_account"),
                    "Amount paid": p["props"].get("amount_paid"),
                    "Currency": p["props"].get("payment_currency"),
                    "Format": p["props"].get("payment_format"),
                }
                for p in payments
            ],
            hide_index=True,
            width="stretch",
            column_config={"Amount paid": st.column_config.NumberColumn(format="localized")},
        )
        notices(detail["notices"])
