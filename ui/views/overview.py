"""Overview: alert volume by typology and the most recent alerts."""

import streamlit as st
from shared import TYPOLOGY_INFO, call, client, go_to, notices, page_header

page_header(
    "Monitoring",
    "Overview",
    "Typology alerts raised on the monitored payment graph. Open an alert to review its "
    "accounts and payments.",
)

summary = call("Loading alert volumes…", client().alert_summary)
if summary is not None:
    rows = summary["typologies"]
    by_typology = {r["typology"]: r for r in rows}
    total = sum(r["alerts"] for r in rows)
    cols = st.columns(5)
    cols[0].metric("Open alerts", f"{total:,}")
    for col, typology in zip(cols[1:], TYPOLOGY_INFO, strict=True):
        count = by_typology.get(typology, {}).get("alerts", 0)
        col.metric(typology, f"{count:,}", help=TYPOLOGY_INFO[typology])

    st.space("small")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Latest alerts", anchor=False)
        latest = call("Loading latest alerts…", client().alert_queue, None, 0, 10)
        if latest is not None:
            table = [
                {
                    "Alert ID": a["id"],
                    "Typology": a["props"].get("typology"),
                    "Window start": a["props"].get("window_start"),
                    "Accounts": len(a["props"].get("flagged_accounts") or []),
                    "Summary": a["props"].get("summary"),
                }
                for a in latest["alerts"]
            ]
            event = st.dataframe(
                table,
                hide_index=True,
                width="stretch",
                on_select="rerun",
                selection_mode="single-row",
                key="overview_latest",
                column_config={
                    "Summary": st.column_config.TextColumn(width="large"),
                    "Accounts": st.column_config.NumberColumn(width="small"),
                },
            )
            picked = event.selection.rows if event else []
            st.caption("Select a row to open the alert in the queue.")
            if picked:
                go_to("views/alert_queue.py", queue_alert_id=table[picked[0]]["Alert ID"])
    with right:
        st.subheader("Typology coverage", anchor=False)
        st.dataframe(
            [
                {
                    "Typology": typology,
                    "Alerts": by_typology.get(typology, {}).get("alerts", 0),
                    "Accounts": by_typology.get(typology, {}).get("accounts", 0),
                    "Latest window end": by_typology.get(typology, {}).get("latest"),
                }
                for typology in TYPOLOGY_INFO
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("What each typology means"):
            for typology, text in TYPOLOGY_INFO.items():
                st.markdown(f"**{typology}** — {text}")
    notices(summary["notices"])
