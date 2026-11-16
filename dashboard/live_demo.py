"""Live GPU demo panel: protected start button, run status, the original validation events and the live
Worker / GPU autoscaling view. Everything shown comes from events the Kaggle batch reported."""
import pandas as pd
import streamlit as st

import api_client
import ui

ACTIVE = ("submitting", "queued", "running")
VALIDATION_EVENTS = ["notebook_started", "environment_ready", "model_located", "model_ready",
                     "model_validation_started", "worker_validation_completed", "modelcheck_completed",
                     "model_validation_completed"]
PHASE_TEXT = {None: "Initializing — no detailed progress reported yet", "initializing": "Initializing notebook",
              "loading_model": "Loading model", "model_ready": "Model server ready (/health 200)",
              "validating": "Running real model validation", "validated": "Validation finished"}
RESULT_TEXT = {"pending": "⏳ Pending", "passed": "✅ Passed", "failed": "❌ Failed",
               "incomplete": "⚠️ Incomplete — Kaggle finished but no passing validation event was received"}
SCALING_TEXT = {
    None: "⏳ Not started", "running": "🔵 Running", "passed": "✅ PASSED", "degraded": "🟠 DEGRADED",
    "failed": "❌ FAILED",
    "incomplete": "⚠️ Incomplete — the batch ended without a scaling result"}
MODES = {"light": "Light — 2 workers / 1 GPU", "high_concurrency": "High Concurrency — 2→8→2 workers / 1→2→1 GPU"}


def na(value, fmt: str = "{}", suffix: str = "") -> str:
    return "N/A" if value is None else fmt.format(value) + suffix


def chart_frames(points: list[dict]) -> dict[str, pd.DataFrame]:
    """One frame per chart, indexed by seconds since the first snapshot. Missing readings stay gaps."""
    if not points:
        return {}
    df = pd.DataFrame(points).set_index("t")

    def pick(rename: dict[str, str]) -> pd.DataFrame:
        cols = {k: v for k, v in rename.items() if k in df and df[k].notna().any()}
        return df[list(cols)].rename(columns=cols).astype(float) if cols else pd.DataFrame()

    frames = {
        "Queue depth": pick({"pending": "Pending", "running": "Running"}),
        "Workers": pick({"actual_workers": "Actual workers", "desired_workers": "Desired workers"}),
        "GPU model instances ready": pick({"gpu_instances_ready": "Ready instances"}),
        "Inference requests served": pick({"gpu0_requests": "GPU 0", "gpu1_requests": "GPU 1"}),
        "GPU utilization %": pick({"gpu0_utilization_pct": "GPU 0", "gpu1_utilization_pct": "GPU 1"}),
        "GPU memory used (MB)": pick({"gpu0_memory_mb": "GPU 0", "gpu1_memory_mb": "GPU 1"}),
    }
    return {name: frame for name, frame in frames.items() if not frame.empty}


def _events_table(token: str, run_id: str) -> None:
    events = api_client.demo_events(token, run_id, VALIDATION_EVENTS)
    if not events:
        st.caption("No events received yet.")
        return
    rows = [{"Received": ui.fmt_ts(e["received_at"]), "Event": e["event_type"],
             "Details": ", ".join(f"{k}={v}" for k, v in e["payload"].items())} for e in events]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _gpu_card(col, title: str, gpu: dict | None, requests, inflight) -> None:
    gpu = gpu or {}
    with col.container(border=True):
        st.markdown(f"**{title}** · `{gpu.get('state') or 'N/A'}` · port {na(gpu.get('port'))}")
        a, b = st.columns(2)
        a.metric("Memory used", na(gpu.get("memory_used_mb"), "{:,}", " MB"))
        b.metric("Utilization", na(gpu.get("utilization_pct"), "{}", "%"))
        a.metric("Requests served", na(requests))
        b.metric("In flight", na(inflight))
        if gpu.get("state") == "READY" and not requests:
            st.caption("READY · 0 requests so far — not yet serving traffic")


def _scaling_overview(state: dict) -> None:
    snap = state["snapshot"] or {}
    st.markdown("#### Scaling overview")
    if state["telemetry_delayed"]:
        st.warning(f"Telemetry delayed — last event {state['seconds_since_last_event']:.0f}s ago. "
                   "Values below may be stale.")
    c = st.columns(6)
    c[0].metric("Actual / desired workers", f"{na(snap.get('actual_workers'))} / {na(snap.get('desired_workers'))}")
    c[1].metric("GPU instances ready", f"{na(snap.get('gpu_instances_ready'))} / 2")
    c[2].metric("Pending", na(snap.get("pending")))
    c[3].metric("Running", na(snap.get("running")))
    c[4].metric("Completed", f"{na(snap.get('completed'))} / {na(snap.get('total') or state.get('units'))}")
    c[5].metric("Failed", na(snap.get("failed")))
    st.markdown(f"Scaling state: **{state['scaling_state'] or 'N/A'}**"
                + (" · 🔴 LIVE" if state["live"] else ""))
    by_gpu, inflight = snap.get("requests_by_gpu") or {}, snap.get("inflight_by_gpu") or {}
    g0, g1 = st.columns(2)
    _gpu_card(g0, "GPU 0", snap.get("gpu0"), by_gpu.get("gpu0"), inflight.get("gpu0"))
    _gpu_card(g1, "GPU 1", snap.get("gpu1"), by_gpu.get("gpu1"), inflight.get("gpu1"))


def _live_metrics(points: list[dict]) -> None:
    frames = chart_frames(points)
    if not frames:
        return
    st.markdown("#### Live metrics")
    st.caption("Seconds since the first snapshot; every point is a real reading from the Kaggle batch.")
    names = list(frames)
    for i in range(0, len(names), 2):
        cols = st.columns(2)
        for col, name in zip(cols, names[i:i + 2]):
            with col:
                st.caption(name)
                st.line_chart(frames[name], height=180)


def _timeline(state: dict) -> None:
    st.markdown("#### Scaling event timeline")
    if not state["timeline"]:
        st.caption("No scaling events yet.")
        return
    rows = [{"Time": ui.fmt_ts(e["received_at"]), "Event": e["event_type"], "What happened": e["label"]}
            for e in state["timeline"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _summary(state: dict) -> None:
    st.markdown("#### Run summary and evidence")
    verdict = SCALING_TEXT.get(state["scaling_status"], state["scaling_status"])
    st.markdown(f"Scaling verdict: **{verdict}** — separate from the model validation result above.")
    failure, summary = state["failure"], state["summary"]
    if failure:
        st.error(f"{failure.get('error_code')} during `{failure.get('phase')}`: {failure.get('message') or ''}")
    if not summary:
        st.caption("The summary appears once the scaling experiment finishes.")
        return
    if summary.get("reasons"):
        st.warning("Not a full pass: " + ", ".join(summary["reasons"]))
    rows = [
        ("Tasks completed / total", f"{na(summary.get('completed'))} / {na(summary.get('total'))}"),
        ("Failed tasks · retries", f"{na(summary.get('failed'))} · {na(summary.get('retries'))}"),
        ("P50 / P95 latency", f"{na(summary.get('p50_seconds'), '{:.1f}', ' s')} / "
                              f"{na(summary.get('p95_seconds'), '{:.1f}', ' s')}"),
        ("Throughput", na(summary.get("throughput_per_min"), "{:.1f}", " tasks/min")),
        ("Peak workers → final workers", f"{na(summary.get('peak_workers'))} → {na(summary.get('final_workers'))}"),
        ("Confirmed worker levels", ", ".join(map(str, summary.get("confirmed_worker_levels") or [])) or "N/A"),
        ("Peak GPU instances → final", f"{na(summary.get('peak_gpu_instances'))} → "
                                        f"{na(summary.get('final_gpu_instances'))}"),
        ("Inference requests GPU 0 / GPU 1", f"{na(summary.get('gpu0_inference_requests'))} / "
                                             f"{na(summary.get('gpu1_inference_requests'))}"),
        ("GPU 1 load time", na(summary.get("gpu1_load_seconds"), "{:.1f}", " s")),
        ("GPU 1 stopped cleanly", na(summary.get("gpu1_stopped_cleanly"))),
        ("Worker scale-out / scale-in", f"{na(summary.get('scale_out_seconds'), '{:.1f}', ' s')} / "
                                        f"{na(summary.get('scale_in_seconds'), '{:.1f}', ' s')}"),
        ("Undelivered telemetry events", na(summary.get("telemetry_failed_events"))),
    ]
    st.dataframe(pd.DataFrame(rows, columns=["Measure", "Value"]), hide_index=True, width="stretch")


def render(token: str) -> None:
    with st.container(border=True):
        st.subheader("Live GPU demo")
        st.caption("Runs a real model check and a Worker/GPU autoscaling experiment as a Kaggle GPU **batch**. "
                   "Kaggle queue time is not guaranteed and the model is not online after the batch ends; "
                   "events and results stay here.")
        try:
            runs = api_client.demo_runs(token)
        except api_client.ApiError as exc:
            st.error(str(exc))
            return
        busy = any(r["status"] in ACTIVE for r in runs)
        c1, c2, c3 = st.columns([3, 1, 1])
        mode = c1.radio("Mode", list(MODES), format_func=MODES.get, horizontal=True, disabled=busy, key="demo_mode")
        units = c2.selectbox("Synthetic tasks", [50, 100, 150], index=1, disabled=busy or mode == "light",
                             key="demo_units", help="High Concurrency only; Light always uses 10.")
        c3.write("")
        if c3.button("▶ Start Live Demo", type="primary", disabled=busy):
            try:
                api_client.demo_start(token, mode, units if mode == "high_concurrency" else None)
                st.rerun()
            except api_client.ApiError as exc:
                st.error({409: "A demo is already queued or running.",
                          429: str(exc)}.get(exc.status, f"Start failed: {exc}"))
        if not runs:
            return
        run_id = st.selectbox("Run", [r["run_id"] for r in runs], key="demo_run")
        _run_panel(token, run_id, any(r["run_id"] == run_id and r["status"] in ACTIVE for r in runs))


def _run_panel(token: str, run_id: str, active: bool) -> None:
    @st.fragment(run_every=3 if active else None)
    def panel() -> None:
        try:
            status = api_client.demo_status(token, run_id)
            state = api_client.demo_state(token, run_id)
            points = api_client.demo_metrics(token, run_id) if state["snapshot"] else []
        except api_client.ApiError as exc:
            st.error(str(exc))
            return
        if active and status["status"] not in ACTIVE:
            st.rerun(scope="app")  # finished: re-enable the start button and stop polling
        icon = {"submitting": "🟡", "queued": "🟠", "running": "🔵", "complete": "🟢",
                "failed": "🔴", "cancelled": "⚫"}.get(status["status"], "•")
        st.markdown(f"{icon} **{status['status']}** · Kaggle: `{status['kaggle_status'] or '—'}` · "
                    f"mode: `{status.get('mode') or '—'}` · events: {status['event_count']} · "
                    f"started {ui.fmt_ts(status['created_at'])} · last event {ui.fmt_ts(status['last_event_at'])}")
        if status["status"] == "queued":
            st.caption("Waiting for Kaggle to schedule the GPU batch; queue time is not guaranteed.")
        elif status["status"] == "running":
            st.caption(f"Phase: {PHASE_TEXT.get(status['phase'], status['phase'])}")
        st.markdown(f"Model: `{status['model']}` · Validation result: {RESULT_TEXT[status['result']]}")
        if status["error"]:
            st.error(status["error"])
        st.markdown("#### Validation events")
        _events_table(token, run_id)
        if state["scaling_status"] is None and not state["timeline"]:
            if status["status"] in ACTIVE and status.get("mode"):
                st.caption("Scaling experiment starts after model validation passes.")
            return
        _scaling_overview(state)
        _live_metrics(points)
        _timeline(state)
        _summary(state)

    panel()
