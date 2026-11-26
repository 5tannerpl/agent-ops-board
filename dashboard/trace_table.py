"""Display only the operational fields of an authorised span projection."""
import pandas as pd
import streamlit as st


def render(spans):
    if not spans:
        st.info("No spans on this page.")
        return
    st.dataframe(pd.DataFrame([{
        "Span": s["span_name"], "Unit": s["unit_id"], "Worker": s["worker_id"],
        "Trace ID": s["trace_id"], "Span ID": s["span_id"], "Parent span ID": s["parent_span_id"],
        "Start (UTC)": pd.to_datetime(s["start_time"], unit="s", utc=True),
        "Duration (ms)": s["duration_ms"], "Status": s["status"],
    } for s in spans]), hide_index=True, width="stretch")
