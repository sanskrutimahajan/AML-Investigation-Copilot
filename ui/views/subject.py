"""Subject profile: one customer's KYC record, accounts, activity, alerts and watchlist matches."""

from typing import Any

import streamlit as st
from shared import (
    SOURCE_LABELS,
    call,
    client,
    go_to,
    key_values,
    notices,
    page_header,
    scenario_label,
)

from tracegraph.api.client import SUBJECT_ID

EDGE_LABELS = {"SAME_AS": "Model match (SAME_AS)", "POSSIBLE_SAME_AS": "Possible match (review)"}

page_header(
    "Investigation",
    "Subject profile",
    "Customer due-diligence view: KYC record, accounts, payment activity, typology alerts and "
    "links to sanctions or offshore records.",
)

with st.form("subject_search", border=False):
    row = st.columns([5, 1], vertical_alignment="bottom")
    query = row[0].text_input(
        "Customer ID, account ID or name",
        value=st.session_state.get("subject_query", ""),
        placeholder="e.g. cust-8005B3110, 19329:810E5B020 or Finane Holdings",
    )
    searched = row[1].form_submit_button("Search", type="primary", width="stretch")
if searched:
    st.session_state["subject_query"] = query.strip()
    st.session_state.pop("subject_id", None)

query = st.session_state.get("subject_query", "").strip()
subject_id: str | None = st.session_state.get("subject_id")
if query and not subject_id:
    if SUBJECT_ID.match(query):
        subject_id = query
    elif len(query) < 2:
        st.warning("Enter at least two characters of a name.")
    else:
        found = call("Searching customers…", client().screen, query, 10)
        customers = [r for r in (found or {}).get("results", []) if r["type"] == "Customer"]
        if found is not None and not customers:
            st.info("No customer matches that name. Try Name screening for watchlist records.")
        elif customers:
            labels = {
                f"{c['name']}  ·  {c['id']}  ·  {c['props'].get('country') or '—'}": c["id"]
                for c in customers
            }
            pick = st.columns([5, 1], vertical_alignment="bottom")
            chosen = pick[0].selectbox(f"{len(customers)} matching customers", list(labels))
            if pick[1].button("Open", width="stretch"):
                st.session_state["subject_id"] = labels[chosen]
                st.rerun()

if subject_id:
    profile = call("Loading profile…", client().subject, subject_id)
    if profile is not None:
        customer = profile["customer"]
        props: dict[str, Any] = customer["props"]
        links = profile["links"]
        entities = {e["id"]: e["props"] for e in profile["linked_entities"]}
        received = [a for a in profile["activity"] if a["props"].get("direction") == "received"]
        sent = [a for a in profile["activity"] if a["props"].get("direction") == "sent"]

        st.divider()
        st.subheader(props.get("name") or customer["id"], anchor=False)
        badges = st.container(horizontal=True, gap="small")
        with badges:
            st.badge(customer["id"], color="gray")
            st.badge((props.get("customer_type") or "unknown").title(), color="blue")
            if props.get("country"):
                st.badge(props["country"], color="gray")
            if props.get("synthetic"):
                st.badge("Synthetic record", color="gray", icon=":material/science:")
            if profile["alerts"]:
                st.badge(f"{len(profile['alerts'])} alerts", color="red",
                         icon=":material/warning:")  # fmt: skip
            if any(link["type"] == "SAME_AS" for link in links):
                st.badge("Watchlist link", color="orange", icon=":material/gpp_maybe:")

        metrics = st.columns(5)
        metrics[0].metric("Accounts", len(profile["accounts"]))
        totals = profile.get("totals") or {}
        metrics[1].metric("Payments received", f"{totals.get('received', {}).get('payments', 0):,}")
        metrics[2].metric("Payments sent", f"{totals.get('sent', {}).get('payments', 0):,}")
        metrics[3].metric("Typology alerts", len(profile["alerts"]))
        metrics[4].metric("Watchlist links", len(links))

        tabs = st.tabs(["Profile", "Accounts", "Activity", "Alerts", "Watchlist and offshore"])
        with tabs[0]:
            is_person = props.get("customer_type") == "person"
            key_values(
                [
                    ("Customer ID", customer["id"]),
                    ("Name", props.get("name")),
                    ("Customer type", props.get("customer_type")),
                    ("Source entity type", props.get("ibm_entity_type")),
                    ("Country", props.get("country")),
                    ("Date of birth" if is_person else "Incorporation date",
                     props.get("date_of_birth") if is_person else props.get("incorporation_date")),
                    ("Address", props.get("address")),
                    ("Synthetic record", props.get("synthetic")),
                ]
            )  # fmt: skip
        with tabs[1]:
            st.dataframe(
                [
                    {
                        "Account": a["id"],
                        "Account number": a["props"].get("account_number"),
                        "Bank ID": a["props"].get("bank_id"),
                        "Bank": a["props"].get("bank_name"),
                    }
                    for a in profile["accounts"]
                ],
                hide_index=True,
                width="stretch",
            )
        with tabs[2]:
            st.markdown("**Activity by account, direction and currency**")
            cap = profile.get("activity_rows_per_direction")
            if cap and max(len(received), len(sent)) >= cap:
                st.caption(f"Showing the {cap} largest groups per direction; the payment "
                           "counts above cover every account.")  # fmt: skip
            st.dataframe(
                [
                    {
                        "Account": a["props"].get("account_id"),
                        "Direction": (a["props"].get("direction") or "").title(),
                        "Currency": a["props"].get("currency"),
                        "Payments": a["props"].get("transactions"),
                        "Total": a["props"].get("total"),
                        "First": a["props"].get("first"),
                        "Last": a["props"].get("last"),
                    }
                    for a in profile["activity"]
                ],
                hide_index=True,
                width="stretch",
                column_config={"Total": st.column_config.NumberColumn(format="localized")},
            )
            st.markdown("**Largest payments**")
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
                    for p in profile["payments"]
                ],
                hide_index=True,
                width="stretch",
                column_config={"Amount paid": st.column_config.NumberColumn(format="localized")},
            )
        with tabs[3]:
            if not profile["alerts"]:
                st.info("No typology alert flags this customer's accounts.")
            else:
                rows = [
                    {
                        "Alert ID": a["id"],
                        "Scenario": scenario_label(a["props"].get("typology")),
                        "Window start": a["props"].get("window_start"),
                        "Summary": a["props"].get("summary"),
                    }
                    for a in profile["alerts"]
                ]
                st.dataframe(rows, hide_index=True, width="stretch",
                             column_config={"Summary": st.column_config.TextColumn(
                                 width="large")})  # fmt: skip
                pick = st.columns([3, 1, 1], vertical_alignment="bottom")
                alert_id = pick[0].selectbox("Alert", [r["Alert ID"] for r in rows])
                if pick[1].button("Review alert", width="stretch"):
                    go_to("views/alert_queue.py", queue_alert_id=alert_id)
                if pick[2].button("Draft SAR", width="stretch"):
                    go_to("views/sar.py", sar_alert_id=alert_id)
        with tabs[4]:
            if not links:
                st.info(
                    "No links to sanctions or offshore records. Entity resolution writes "
                    "model matches and possible matches here."
                )
            else:
                st.dataframe(
                    [
                        {
                            "Match": EDGE_LABELS.get(link["type"], link["type"]),
                            "Record": entities.get(link["props"].get("entity_id"), {}).get("name"),
                            "Source": SOURCE_LABELS.get(
                                entities.get(link["props"].get("entity_id"), {}).get("source"),
                                "—",
                            ),
                            "Record ID": link["props"].get("entity_id"),
                            "Probability": link["props"].get("probability"),
                            "Method": link["props"].get("method"),
                            "Synthetic link": link["props"].get("synthetic_link"),
                        }
                        for link in links
                    ],
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "Probability": st.column_config.ProgressColumn(
                            min_value=0.0, max_value=1.0, format="%.3f"
                        )
                    },
                )
        notices(profile["notices"])
