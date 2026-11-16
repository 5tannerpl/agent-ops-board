"""Live GPU demo panel: protected start button, run status and the events Kaggle reported."""
import pandas as pd
import streamlit as st

import api_client
import ui

ACTIVE = ("submitting", "queued", "running")


def _events_table(token: str, run_id: str) -> None:
    events = api_client.demo_events(token, run_id)
    if not events:
        st.caption("No events received yet.")
        return
    rows = [{"Received": ui.fmt_ts(e["received_at"]), "Event": e["event_type"],
             "Details": ", ".join(f"{k}={v}" for k, v in e["payload"].items())} for e in events]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def render(token: str) -> None:
    with st.container(border=True):
        st.subheader("Live GPU demo")
        st.caption("Runs a real model check as a Kaggle GPU **batch**. Kaggle queue time is not guaranteed and "
                   "the model is not online after the batch ends; events and results stay here.")
        try:
            runs = api_client.demo_runs(token)
        except api_client.ApiError as exc:
            st.error(str(exc))
            return
        busy = any(r["status"] in ACTIVE for r in runs)
        if st.button("▶ Start Live Demo", type="primary", disabled=busy):
            try:
                api_client.demo_start(token)
                st.rerun()
            except api_client.ApiError as exc:
                st.error({409: "A demo is already queued or running.",
                          429: str(exc)}.get(exc.status, f"Start failed: {exc}"))
        if not runs:
            return
        run_id = st.selectbox("Run", [r["run_id"] for r in runs], key="demo_run")
        _run_panel(token, run_id, any(r["run_id"] == run_id and r["status"] in ACTIVE for r in runs))


def _run_panel(token: str, run_id: str, active: bool) -> None:
    @st.fragment(run_every=5 if active else None)
    def panel() -> None:
        try:
            status = api_client.demo_status(token, run_id)
        except api_client.ApiError as exc:
            st.error(str(exc))
            return
        if active and status["status"] not in ACTIVE:
            st.rerun(scope="app")  # finished: re-enable the start button and stop polling
        icon = {"submitting": "🟡", "queued": "🟠", "running": "🔵", "complete": "🟢",
                "failed": "🔴", "cancelled": "⚫"}.get(status["status"], "•")
        st.markdown(f"{icon} **{status['status']}** · Kaggle: `{status['kaggle_status'] or '—'}` · "
                    f"events: {status['event_count']} · started {ui.fmt_ts(status['created_at'])}")
        if status["error"]:
            st.error(status["error"])
        _events_table(token, run_id)

    panel()
