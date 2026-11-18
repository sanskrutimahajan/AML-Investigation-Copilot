"""Alert queue: filter and page through alerts, then review one alert's accounts and payments."""

from typing import Any

import streamlit as st
from shared import (
    TYPOLOGY_INFO,
    call,
    client,
    go_to,
    key_values,
    money,
    notices,
    page_header,
    typology_badge,
)

from tracegraph.api.client import ALERT_ID, SUBJECT_ID

PAGE_SIZE = 25

page_header(
    "Monitoring",
    "Alert queue",
    "Alerts newest first. Filter by typology or look up the alerts on one customer or account.",
)

st.session_state.setdefault("queue_offset", 0)
filters = st.columns([3, 2, 1], vertical_alignment="bottom")
with filters[0]:
    typology = st.segmented_control(
        "Typology",
        ["All", *TYPOLOGY_INFO],
        default="All",
        key="queue_typology",
        on_change=lambda: st.session_state.update(queue_offset=0),
    )
with filters[1]:
    subject = st.text_input(
        "Customer or account ID", placeholder="e.g. 19329:810E5B020", key="queue_subject"
    ).strip()
with filters[2]:
    if st.button("Reset filters", width="stretch"):
        for key in ("queue_typology", "queue_subject", "queue_alert_id"):
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
    result = call("Loading alerts…", client().alert_queue, chosen, offset, PAGE_SIZE)

alerts: list[dict[str, Any]] = result["alerts"] if result else []
if subject and chosen:
    alerts = [a for a in alerts if a["props"].get("typology") == chosen]

if result is not None:
    if not alerts:
        st.info("No alerts match these filters.")
    else:
        table = [
            {
                "Alert ID": a["id"],
                "Typology": a["props"].get("typology"),
                "Window start": a["props"].get("window_start"),
                "Window end": a["props"].get("window_end"),
                "Accounts": len(a["props"].get("flagged_accounts") or []),
                "Summary": a["props"].get("summary"),
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
                "Summary": st.column_config.TextColumn(width="large"),
                "Accounts": st.column_config.NumberColumn(width="small"),
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
        head = st.columns([4, 1, 1], vertical_alignment="center")
        with head[0]:
            st.subheader(f"Alert review · {alert_id}", anchor=False)
            typology_badge(props.get("typology"))
        accounts = detail["accounts"]
        payments = detail["payments"]
        if head[1].button("Draft SAR", type="primary", width="stretch",
                          icon=":material/description:"):  # fmt: skip
            go_to("views/sar.py", sar_alert_id=alert_id)
        owner_ids = sorted({a["props"].get("owner_customer_id") for a in accounts} - {None})
        if head[2].button("Open subject", width="stretch", disabled=not owner_ids,
                          icon=":material/badge:"):  # fmt: skip
            go_to("views/subject.py", subject_query=owner_ids[0], subject_id=None)

        info, totals = st.columns([3, 2], gap="large")
        with info:
            key_values(
                [
                    ("Summary", props.get("summary")),
                    ("Typology definition", TYPOLOGY_INFO.get(props.get("typology", ""))),
                    ("Window", f"{props.get('window_start')} to {props.get('window_end')}"),
                    ("Detection thresholds", props.get("detection_thresholds")),
                ]
            )
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
