"""Dashboard (page A): live load-test progress, worker health and the intake-unit list."""
import time

import pandas as pd
import streamlit as st

import api_client
import ui

ui.page_header("Agent Ops Board")
ui.inject_area_css()

STATUSES = ["PENDING", "RUNNING", "WAITING_RETRY", "COMPLETED", "FAILED", "MANUAL_REVIEW"]
INTENTS = ["PAYMENT_VERIFICATION", "DEBT_DISPUTE", "PAYMENT_ARRANGEMENT", "HARDSHIP_NOTIFICATION",
           "ACCOUNT_BALANCE_REQUEST", "COMPLAINT", "CEASE_CONTACT_REQUEST", "DETAILS_UPDATE", "OTHER"]
SCENARIOS = {"multi_channel_350": "Multi-channel, arrivals spread over the window",
             "burst_350": "Burst: all units injected at t=0 (drain time)"}

snapshot = ui.api_guard(api_client.dashboard)
run = snapshot["run"]
active = bool(run and run["state"] == "RUNNING")

# ---- controls ---------------------------------------------------------------
with st.container(border=True):
    c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
    scenario = c1.selectbox("Scenario", list(SCENARIOS), format_func=SCENARIOS.get, disabled=active)
    units = c2.number_input("Units", 1, 5000, 350, disabled=active)
    workers_n = c3.number_input("Workers", 1, 8, 2, disabled=active)
    window = c4.number_input("Arrival window (s)", 0, 7200, 600, disabled=active or scenario == "burst_350")
    deadline = c5.number_input("Deadline (s)", 1, 7200, 600, disabled=active)
    faults = st.checkbox("Inject controlled failures (demo: model timeout, invalid JSON, handoff failure, "
                         "worker crash)", disabled=active)
    b1, b2, _ = st.columns([1, 1, 6])
    if b1.button("▶ Start Load Test", type="primary", disabled=active):
        try:
            started = api_client.start_load_test(
                scenario=scenario, target_units=int(units), arrival_window_seconds=float(window),
                completion_deadline_seconds=float(deadline), worker_count=int(workers_n), inject_faults=faults)
            st.toast(f"Started {started['test_run_id']}")
            st.rerun()
        except api_client.ApiError as exc:
            st.error(str(exc))
    if b2.button("■ Stop Test", disabled=not active, help="Stops producing new units; queued jobs still finish."):
        try:
            api_client.stop_load_test(run["test_run_id"])
            st.rerun()
        except api_client.ApiError as exc:
            st.error(str(exc))

with st.expander("Submit one synthetic message (optional)"):
    with st.form("submit_message", clear_on_submit=True):
        channel = st.selectbox("Channel", ["email", "chat", "voice"])
        subject = st.text_input("Subject (email)")
        body = st.text_area("Body / customer text", "I already paid $2,000. I dispute the balance and "
                            "request monthly instalments.")
        if st.form_submit_button("Submit"):
            payload = {"channel": channel, "subject": subject, "body": body}
            if channel != "email":
                payload["messages"] = [{"speaker": "Customer", "text": body}]
            try:
                st.success(f"Queued {api_client.submit_message(**payload)['unit_id']}")
            except api_client.ApiError as exc:
                st.error(str(exc))


# ---- live section -----------------------------------------------------------
@st.fragment(run_every=2 if active else 5)
def live_section() -> None:
    snap = ui.api_guard(api_client.dashboard)
    run_now = snap["run"]
    if bool(run_now and run_now["state"] == "RUNNING") != active:
        st.rerun()  # run started/finished: re-evaluate controls and refresh interval
    m, counts = snap["metrics"], snap["metrics"]["counts"]

    with st.container(key="area_dashboard"):
        ui.area_header("dashboard")
        ui.model_banner(snap["model"])
        if run_now:
            st.markdown(
                f"**{run_now['test_run_id']}** · {ui.badge(run_now['state'])} · scenario `{run_now['scenario']}` · "
                f"workers {run_now['worker_count']} · started {ui.fmt_ts(run_now['started_at'])} · "
                f"elapsed **{ui.fmt_dur(run_now['elapsed_seconds'])}** of "
                f"{ui.fmt_dur(run_now['completion_deadline_seconds'])} target")
            st.progress(min(m["completion_pct"] / 100, 1.0),
                        text=f"{m['completion_pct']}% completed ({counts['completed']}/{m['target_count']}) · "
                             f"produced {run_now['produced_count']}/{run_now['target_count']}")
            if run_now["deadline_exceeded"]:
                st.error("Completion deadline exceeded — the all-completed acceptance target can no longer be met.")
            acc = run_now["acceptance"]
            if acc:
                detail = (f"completed {counts['completed']}/{run_now['target_count']} in "
                          f"{ui.fmt_dur(run_now['elapsed_seconds'])} (deadline {ui.fmt_dur(acc['deadline_seconds'])})")
                if acc["passed"]:
                    st.success(f"Acceptance target met: {detail}. "
                               "A pass counts only for a real model backend on the recorded hardware.")
                else:
                    st.error(f"Acceptance target NOT met: {detail}. Failed/manual-review units remain visible below.")
            if run_now["stop_requested"] and run_now["state"] == "RUNNING":
                st.warning("Stop requested: no new units are produced; queued jobs continue.")
        else:
            st.info("No load test yet. Showing all intake units.")

        cols = st.columns(6)
        for col, (label, key) in zip(cols, [("Received", "received"), ("Completed", "completed"),
                                             ("Running", "running"), ("Pending", "pending"),
                                             ("Waiting Retry", "waiting_retry"), ("Failed", "failed")]):
            col.metric(label, counts[key])
        if counts["manual_review"]:
            st.warning(f"{counts['manual_review']} unit(s) in MANUAL_REVIEW (counted separately, still visible).")
        k = st.columns(6)
        k[0].metric("Completion", f"{m['completion_pct']}%")
        k[1].metric("Completed / min", m["completed_per_min"], help=f"Required: {m['required_per_min']}/min")
        k[2].metric("Latency P50", ui.fmt_dur(m["latency_p50_s"]))
        k[3].metric("Latency P95", ui.fmt_dur(m["latency_p95_s"]))
        k[4].metric("Queue depth", m["queue_depth"])
        est = m["estimated_remaining_seconds"]
        k[5].metric("Est. remaining", ui.fmt_dur(est) if est is not None else "—",
                    help="Estimate from measured throughput only; shown once ≥5 units completed.")
        st.caption(f"Retry attempts: {m['retry_attempts']} across {m['units_with_retries']} unit(s). "
                   "KPIs count each unit once.")


    with st.container(key="area_workers"):
        ui.area_header("workers")
        rows = [{"Worker": w["worker_id"], "Status": ui.badge(w["state"]), "Health": w["health"],
                 "Current Unit": w["current_unit_id"] or "—", "Current Step": w["current_step"] or "—",
                 "Attempt": str(w["attempt"]) if w["current_unit_id"] and w["attempt"] else "—",
                 "Step elapsed": ui.fmt_dur(w["step_elapsed_seconds"]),
                 "Heartbeat": ui.fmt_age(w["heartbeat_age_seconds"])} for w in snap["workers"]]
        if rows:
            prev = st.session_state.get("worker_ids", [])
            ev = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", on_select="rerun",
                              selection_mode="single-row", key="worker_table")
            st.session_state["worker_ids"] = [r["Worker"] for r in rows]
            if ev.selection.rows and ev.selection.rows[0] < len(prev):
                ui.open_worker(prev[ev.selection.rows[0]])
            st.caption("Select a row to open the Worker Monitor.")
        else:
            sup = snap["supervisor"]
            if not sup["alive"]:
                st.error("No worker supervisor is running against the API's database, so queued units will not be "
                         "processed. Start it from the repo root with `python -m workers.supervisor --workers 2` "
                         "(or `python run_demo.py` to start everything). It must use the same `runtime/` folder / "
                         f"`AOB_DB_PATH` as the API: `{sup['db_path']}`")
            else:
                st.warning(f"Supervisor (pid {sup['pid']}) is running but no worker has registered. Check the "
                           "supervisor terminal for worker start-up errors.")


live_section()

# ---- intake units -------------------------------------------------------------
def units_body() -> None:
    f = st.columns([2, 1, 1.4, 1.2, 1.6, 1.4])
    q = f[0].text_input("Unit ID contains", key="f_q")
    channel = f[1].selectbox("Channel", ["", "email", "chat", "voice"], key="f_channel")
    status = f[2].selectbox("Status", [""] + STATUSES, key="f_status")
    worker = f[3].text_input("Worker", key="f_worker", placeholder="worker-01")
    intent = f[4].selectbox("Intent", [""] + INTENTS, key="f_intent")
    recent = f[5].selectbox("Created", ["any time", "last 5 min", "last 30 min", "last 24 h"], key="f_recent")
    p = st.columns([1, 1, 1, 3])
    page_size = p[0].selectbox("Page size", [25, 50, 100], index=1, key="f_size")
    only_run = p[2].checkbox("Current run only", value=False, key="f_run", disabled=not run)
    page = p[1].number_input("Page", 1, 10_000, 1, key="f_page")
    after = {"last 5 min": 300, "last 30 min": 1800, "last 24 h": 86400}.get(recent)
    data = ui.api_guard(
        api_client.intake_units, q=q, channel=channel, status=status, worker_id=worker.strip(), intent=intent,
        test_run_id=run["test_run_id"] if only_run and run else None, page=int(page), page_size=page_size,
        created_after=(time.time() - after) if after else None)
    pages = max(1, -(-data["total"] // page_size))
    p[3].caption(f"{data['total']} unit(s) · page {data['page']} of {pages}")
    rows = [{"Unit ID": u["unit_id"], "Channel": u["channel"], "Subject / preview": u["preview"],
             "Status": ui.badge(u["status"]), "Worker": u["worker_id"] or "—",
             "Current Step": u["current_step"] or "—", "Duration": ui.fmt_dur(u["duration_seconds"])}
            for u in data["items"]]
    if not rows:
        st.info("No intake units match.")
        return
    prev = st.session_state.get("unit_ids", [])
    ev = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", on_select="rerun",
                      selection_mode="single-row", key="unit_table")
    st.session_state["unit_ids"] = [u["unit_id"] for u in data["items"]]
    if ev.selection.rows and ev.selection.rows[0] < len(prev):
        ui.open_unit(prev[ev.selection.rows[0]])
    st.caption("Select a row to open the Intake Unit Detail.")


@st.fragment(run_every=5 if active else 15)
def units_section() -> None:
    with st.container(key="area_units"):
        ui.area_header("units")
        units_body()


units_section()
