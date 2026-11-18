"""Overview: alert volume by priority band and scenario, and the highest-priority alerts."""

import streamlit as st
from shared import (
    TYPOLOGY_INFO,
    as_of,
    call,
    client,
    go_to,
    notices,
    page_header,
    replay_notice,
    scenario_label,
)

page_header(
    "Monitoring",
    "Overview",
    "Alerts raised by the transaction-monitoring scenarios, by priority. Work the High band "
    "first; open an alert to review it and start a case.",
)

replay_notice()
summary = call("Loading alert volumes…", client().alert_summary, as_of())
if summary is not None:
    rows = summary["typologies"]
    by_typology = {r["typology"]: r for r in rows}
    bands = summary.get("bands", {})
    tiles = st.columns(4)
    tiles[0].metric("Open alerts", f"{sum(r['alerts'] for r in rows):,}")
    for col, band in zip(tiles[1:], ("High", "Medium", "Low"), strict=True):
        col.metric(f"{band} priority", f"{bands.get(band, 0):,}")

    st.space("small")
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Highest-priority alerts", anchor=False)
        top = call("Loading alerts…", client().alert_queue, None, 0, 10, as_of())
        if top is not None:
            table = [
                {
                    "Priority": f"{a['props'].get('priority_band', '—')} "
                    f"({a['props'].get('priority', '—')})",
                    "Scenario": scenario_label(a["props"].get("typology")),
                    "Customer": ", ".join(a["props"].get("owner_names") or []) or "—",
                    "Alert ID": a["id"],
                }
                for a in top["alerts"]
            ]
            event = st.dataframe(
                table,
                hide_index=True,
                width="stretch",
                on_select="rerun",
                selection_mode="single-row",
                key="overview_latest",
                column_config={"Priority": st.column_config.TextColumn(width=110)},
            )
            picked = event.selection.rows if event else []
            st.caption("Select a row to review the alert in the queue.")
            if picked:
                go_to("views/alert_queue.py", queue_alert_id=table[picked[0]]["Alert ID"])
    with right:
        st.subheader("Scenarios", anchor=False)
        st.dataframe(
            [
                {
                    "Scenario": scenario_label(typology),
                    "Alerts": by_typology.get(typology, {}).get("alerts", 0),
                    "Accounts": by_typology.get(typology, {}).get("accounts", 0),
                }
                for typology in TYPOLOGY_INFO
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("What each scenario looks for"):
            for typology, text in TYPOLOGY_INFO.items():
                st.markdown(f"**{scenario_label(typology)}** — {text}")
    notices(summary["notices"])
