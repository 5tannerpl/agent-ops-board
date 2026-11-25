"""Private deterministic Azure execution, separate from the historical OTLP browser."""
import os
from uuid import uuid4

import streamlit as st

import api_client
import config


def render(token: str):
    deployment = os.getenv("AOB_AZURE_RUN_DEPLOYMENT_ID")
    if not (config.CROSS_CLOUD_ENABLED and deployment):
        return
    with st.container(border=True):
        st.subheader("Azure Agent: deterministic 100-task Run")
        selected = st.session_state.get("azure_local_run")
        if selected:
            _status(token, selected)
        elif config.REMOTE_RUNS_ENABLED:
            pending = st.session_state.get("azure_start_key")
            label = "Recover reservation receipt" if pending else "Start Azure Run"
            if st.button(label, key="azure_reserve"):
                key = pending or uuid4().hex
                st.session_state["azure_start_key"] = key
                try:
                    receipt = api_client.azure_start(token, deployment, key)
                except api_client.ApiError as exc:
                    st.warning(f"Reservation not confirmed ({exc.status or 'connection error'}). "
                               "Recover the receipt using the same operation key.")
                    return
                st.session_state["azure_local_run"] = receipt["run_id"]
                del st.session_state["azure_start_key"]
                st.rerun()
        else:
            st.caption("Remote execution disabled.")


@st.fragment(run_every=5)
def _status(token, local):
    try:
        run = api_client.stored_run(token, local)
    except api_client.ApiError as exc:
        st.warning(f"Stored status unavailable ({exc.status or 'connection error'}).")
        return
    state = run["dispatch"]["state"]
    st.write({"Local Run": local, "Remote Run": run["remote_run_id"], "Dispatch": state,
              "Business outcome": run["business_status"], "Stored binding telemetry": run["telemetry_status"]})
    st.caption("Remote cancellation is not supported. Check source-scoped OTLP history for trace completeness.")
    if state == "uncertain":
        st.warning("The provider outcome is unknown. The global slot remains occupied; do not start again.")
    if state == "terminal" and config.REMOTE_RUNS_ENABLED and st.button("Prepare another Azure Start"):
        del st.session_state["azure_local_run"]
        st.rerun()
