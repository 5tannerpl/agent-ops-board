"""Worker Monitor (page B): what a worker is doing now, its heartbeat, retries and recent history."""
import pandas as pd
import streamlit as st

import api_client
import ui

ui.page_header("Worker Monitor")

worker_id = st.query_params.get("worker_id") or st.session_state.get("worker_id")
ui.back_to_dashboard()
known = [w["worker_id"] for w in ui.api_guard(api_client.workers)]
if not known:
    st.info("No workers registered yet.")
    st.stop()
if worker_id not in known:
    worker_id = st.selectbox("Worker", known)
    if worker_id is None:
        st.stop()
st.session_state["worker_id"] = worker_id
st.query_params["worker_id"] = worker_id


@st.fragment(run_every=2)
def monitor() -> None:
    try:
        w = api_client.worker(worker_id)
        events = api_client.worker_events(worker_id, 100)
    except api_client.ApiError as exc:
        st.error(str(exc))
        return
    st.subheader(worker_id)
    h = st.columns(6)
    ui.stat(h[0], "PID / host", f"{w['pid'] or '—'} / {w['host'] or '—'}")
    ui.stat(h[1], "Health", w["health"])
    ui.stat(h[2], "State", ui.badge(w["state"]))
    ui.stat(h[3], "Last heartbeat", ui.fmt_age(w["heartbeat_age_seconds"]))
    lease = w["lease_expiry"]
    ui.stat(h[4], "Lease expiry", f"in {max(lease - w['server_time'], 0):.0f}s" if lease and w["current_unit_id"] else "—")
    ui.stat(h[5], "Current unit", w["current_unit_id"] or "—")
    if w["state"] in ("UNHEALTHY", "OFFLINE"):
        st.warning(f"Worker is {w['state']} (derived from heartbeat age, last status was {w['stored_state']}). "
                   "Its lease will expire and the job will be recovered by another worker.")
    if w["state"] == "BACKOFF":
        st.info("Worker is in BACKOFF: pausing job consumption after a resource failure.")
    if w["current_unit_id"] and st.button(f"Open {w['current_unit_id']} →", key="open_current"):
        ui.open_unit(w["current_unit_id"])

    ex = w["execution"]
    st.subheader("Current execution" if ex and ex["is_current"] else "Last execution")
    if not ex:
        st.caption("This worker has not processed any unit yet.")
    else:
        c = st.columns(6)
        ui.stat(c[0], "Unit", ex["unit_id"])
        ui.stat(c[1], "Step", ex["step"] or "—")
        ui.stat(c[2], "Attempt", f"{ex['attempt']} / {ex['max_attempts']}")
        ui.stat(c[3], "Started", ui.fmt_ts(ex["started_at"]))
        ui.stat(c[4], "Elapsed", ui.fmt_dur(ex["elapsed_seconds"]))
        ui.stat(c[5], "Last event", ui.fmt_ts(ex["last_event_at"]))
        st.caption(f"Model: {ex['model_name'] or '—'}" + (f" · last error code: `{ex['error_code']}`"
                                                         if ex["error_code"] else ""))
        if st.button(f"Open {ex['unit_id']} →", key="open_exec"):
            ui.open_unit(ex["unit_id"])
        cols = st.columns(len(ex["steps"]))
        for col, s in zip(cols, ex["steps"]):
            col.markdown(f"**{s['step_name']}**  \n{ui.badge(s['status'])}  \n{ui.fmt_dur(s['duration_seconds'])}")
            if s["error_code"]:
                col.caption(f"`{s['error_code']}`")
        st.caption("Stage indicator only: token generation has no known completion percentage.")

    st.subheader("Events and retries")
    if not events:
        st.caption("No events yet.")
        return
    df = pd.DataFrame([{
        "Timestamp": ui.fmt_ts(e["timestamp"]), "Unit": e["unit_id"] or "—", "Step": e["step_name"] or "—",
        "Event": e["event_type"], "Attempt": e["attempt"], "Duration": ui.fmt_dur(
            e["duration_ms"] / 1000 if e["duration_ms"] is not None else None),
        "Error": e["error_code"] or ""} for e in events])
    st.dataframe(df, hide_index=True, width="stretch", height=360)
    problems = [e for e in events if e["error_code"] or e["event_type"] in ("RETRY_SCHEDULED", "LEASE_RECOVERED")]
    for e in problems[:10]:
        with st.expander(f"{ui.fmt_ts(e['timestamp'])} · {e['unit_id'] or '—'} · {e['event_type']}"
                         f" · {e['error_code'] or ''}"):
            st.text(e["message"] or "(no message)")
            d = e["details"]
            if d and "retry_at" in d:
                st.write(f"Retry scheduled at **{ui.fmt_ts(d['retry_at'])}** (backoff {d['backoff_seconds']}s); "
                         f"previous attempt {d['previous_attempt']} → next attempt {d['next_attempt']}.")


monitor()
