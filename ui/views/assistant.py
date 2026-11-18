"""Investigation assistant: questions in plain language, answered with cited graph IDs."""

from typing import Any

import streamlit as st
from shared import call, client, notices, page_header

from tracegraph.api.client import STATUS_LABELS, cited_ids, evidence_rows

EXAMPLES = (
    "How much did account 10:8000588D0 send between 1 and 5 September 2022?",
    "Are there any typology alerts on account 19329:810E5B020?",
    "Who owns account 19329:810E5B020?",
)

page_header(
    "Investigation",
    "Investigation assistant",
    "Ask about a customer, account, payment path, offshore entity or alert. Every sentence "
    "cites the graph records it rests on; unsupported answers are withheld.",
)

with st.expander("Example questions"):
    for i, example in enumerate(EXAMPLES):
        if st.button(example, key=f"example_{i}", type="tertiary"):
            st.session_state["assistant_question"] = example

with st.form("assistant_form"):
    question = st.text_area(
        "Question", key="assistant_question", max_chars=1000, height=90,
        placeholder="e.g. Is there a payment path from account A to account B?",
    )  # fmt: skip
    submitted = st.form_submit_button("Ask", type="primary")
if submitted and question.strip():
    st.session_state["assistant_result"] = call(
        "Retrieving evidence and drafting a cited answer…", client().ask, question.strip()
    )

result: dict[str, Any] | None = st.session_state.get("assistant_result")
if result:
    status = result["status"]
    label = STATUS_LABELS.get(status, status)
    with st.container(border=True):
        st.badge(label, color="green" if status == "answered" else
                 "blue" if status == "ambiguous" else "orange")  # fmt: skip
        sentences = result.get("sentences") or []
        if sentences:
            for sentence in sentences:
                st.markdown(sentence["text"])
                st.caption("Sources: " + ", ".join(f"`{c}`" for c in sentence.get("citations", [])))
        else:
            st.markdown(result["answer"])
        notices(result.get("notices"))
    evidence = result.get("evidence") or []
    if evidence:
        cited = cited_ids(sentences)
        with st.expander(f"Evidence ({len(evidence)} records, {len(cited)} cited)"):
            st.dataframe(evidence_rows(evidence, cited), hide_index=True, width="stretch")
    with st.expander("Query route and trace"):
        st.json({"route": result.get("route"), "links": result.get("links"),
                 "trace": result.get("trace")}, expanded=False)  # fmt: skip
