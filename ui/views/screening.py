"""Name screening: search a name against sanctions, offshore and customer records."""

from typing import Any

import streamlit as st
from shared import SOURCE_LABELS, call, client, go_to, key_values, notices, page_header

page_header(
    "Investigation",
    "Name screening",
    "Fuzzy and semantic name search across sanctions lists (OpenSanctions), the ICIJ Offshore "
    "Leaks and customer records. Candidates are leads for review, not matches.",
)

with st.form("screen_form", border=False):
    row = st.columns([4, 1, 1], vertical_alignment="bottom")
    name = row[0].text_input("Name to screen", placeholder="Person or company name")
    limit = row[1].selectbox("Max candidates", [10, 15, 25], index=0)
    submitted = row[2].form_submit_button("Screen", type="primary", width="stretch")

if submitted:
    if len(name.strip()) < 2:
        st.warning("Enter at least two characters.")
    else:
        st.session_state["screen_result"] = call(
            "Screening…", client().screen, name.strip(), int(limit)
        )

result: dict[str, Any] | None = st.session_state.get("screen_result")
if result is not None:
    hits = result["results"]
    if not hits:
        st.info(f"No candidates for “{result['query']}”.")
    else:

        def source(hit: dict[str, Any]) -> str:
            if hit["type"] == "Customer":
                return "Customer record"
            return SOURCE_LABELS.get(hit["props"].get("source"), "Source record")

        table = [
            {
                "Candidate": h["name"],
                "Record type": source(h),
                "Exact name": h["exact"],
                "Country": ", ".join(h["props"].get("countries") or [])
                or h["props"].get("country")
                or h["props"].get("jurisdiction"),
                "Lists / datasets": ", ".join(h["props"].get("datasets") or []),
                "Matched by": " + ".join(h["matched_by"]),
                "Record ID": h["id"],
            }
            for h in hits
        ]
        st.caption(f"{len(hits)} candidates for “{result['query']}”. Select one to see its record.")
        event = st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key="screen_table",
            column_config={"Exact name": st.column_config.CheckboxColumn(width="small")},
        )
        picked = event.selection.rows if event else []
        if picked:
            hit = hits[picked[0]]
            props = hit["props"]
            with st.container(border=True):
                head = st.columns([4, 1], vertical_alignment="center")
                head[0].markdown(f"**{hit['name']}**  \n{source(hit)} · `{hit['id']}`")
                if hit["type"] == "Customer" and head[1].button("Open profile", width="stretch"):
                    go_to("views/subject.py", subject_query=hit["id"], subject_id=None)
                if hit["type"] == "Customer":
                    key_values(
                        [
                            ("Customer type", props.get("customer_type")),
                            ("Country", props.get("country")),
                            ("Date of birth", props.get("date_of_birth")),
                            ("Incorporation date", props.get("incorporation_date")),
                            ("Synthetic record", props.get("synthetic")),
                        ]
                    )
                else:
                    key_values(
                        [
                            ("Schema", props.get("schema") or props.get("node_type")),
                            ("Topics", props.get("topics")),
                            ("Datasets", props.get("datasets")),
                            ("Countries", props.get("countries")),
                            ("Jurisdiction", props.get("jurisdiction")),
                            ("Birth dates", props.get("birth_dates")),
                            ("Leak", props.get("source_leak")),
                            ("Status", props.get("status")),
                        ]
                    )
    notices(result["notices"])
