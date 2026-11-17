"""Live GPU demo panel: protected start button, run status, the original validation events and the live
Worker / GPU autoscaling view. Everything shown comes from events the Kaggle batch reported."""
import pandas as pd
import streamlit as st

import api_client
import ui

ACTIVE = ("submitting", "queued", "running")
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
        "GPU utilization %": pick({"gpu0_utilization_pct": "GPU 0", "gpu1_utilization_pct": "GPU 1"}),
        "GPU memory used (MB)": pick({"gpu0_memory_mb": "GPU 0", "gpu1_memory_mb": "GPU 1"}),
    }
    return {name: frame for name, frame in frames.items() if not frame.empty}


def _events_table(token: str, run_id: str) -> None:
    events = api_client.demo_validation(token, run_id)
    if not events:
        st.caption("No events received yet.")
        return
    rows = [{"Received": ui.fmt_ts(e["received_at"]), "Event": e["event_type"],
             "Status": e.get("status") or "—"} for e in events]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _gpu_card(col, title: str, gpu: dict | None, units, inflight) -> None:
    gpu = gpu or {}

    with col.container(border=True):
        st.markdown(
            f"**{title}** · `{gpu.get('state') or 'N/A'}` "
            f"· port {na(gpu.get('port'))}"
        )

        a, b = st.columns(2)

        a.metric(
            "Memory used",
            na(gpu.get("memory_used_mb"), "{:,}", " MB")
        )
        b.metric(
            "Utilization",
            na(gpu.get("utilization_pct"), "{}", "%")
        )
        a.metric("Units processed", na(units))
        b.metric("In flight", na(inflight))


def _scaling_overview(state: dict) -> None:
    snap = state["snapshot"] or {}
    st.markdown("#### Scaling overview")
    if state["telemetry_delayed"]:
        st.warning(f"Telemetry delayed — last event {state['seconds_since_last_event']:.0f}s ago. "
                   "Values below may be stale.")
    c = st.columns(5)
    c[0].metric("Actual / desired workers", f"{na(snap.get('actual_workers'))} / {na(snap.get('desired_workers'))}")
    c[1].metric("Pending", na(snap.get("pending")))
    c[2].metric("Running", na(snap.get("running")))
    c[3].metric("Completed", f"{na(snap.get('completed'))} / {na(snap.get('total') or state.get('units'))}")
    c[4].metric("Failed", na(snap.get("failed")))
    st.markdown(f"Scaling state: **{state['scaling_state'] or 'N/A'}**"
                + (" · 🔴 LIVE" if state["live"] else ""))
    by_gpu = snap.get("units_by_gpu") or {}
    inflight = snap.get("inflight_by_gpu") or {}

    g0, g1 = st.columns(2)

    _gpu_card(
        g0, "GPU 0",
        snap.get("gpu0"),
        by_gpu.get("gpu0"),
        inflight.get("gpu0")
    )

    _gpu_card(
        g1, "GPU 1",
        snap.get("gpu1"),
        by_gpu.get("gpu1"),
        inflight.get("gpu1")
    )

    if snap.get("unattributed_units", 0) > 0:
        st.warning(
            f"{snap['unattributed_units']} completed units "
            "have no confirmed GPU attribution."
        )


def _live_metrics(points: list[dict]) -> None:
    frames = chart_frames(points)
    if not frames:
        return
    st.markdown("#### Live metrics")
    st.caption("Seconds since the first snapshot; every point is a real reading from the Kaggle batch.")
    # Larger charts: two wide columns, roughly 1.6:1 on a typical desktop.
    # Streamlit automatically stacks the columns on narrow screens.
    names = list(frames)
    for i in range(0, len(names), 2):
        cols = st.columns(2, gap="large")
        for col, name in zip(cols, names[i:i + 2]):
            with col:
                st.markdown(f"**{name}**")
                st.line_chart(frames[name], height=310, width="stretch")


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
        (
            "Completed units GPU 0 / GPU 1",
            f"{na(summary.get('gpu0_units_completed'))} / "
            f"{na(summary.get('gpu1_units_completed'))}"
        ),
        (
            "Unattributed completed units",
            na(summary.get("unattributed_units"))
        ),
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
                started = api_client.demo_start(
                    token, mode, units if mode == "high_concurrency" else None
                )
                # The new run is not in this render's `runs` list yet.
                # Apply the selection before creating the selectbox on rerun.
                new_id = started.get("run_id") if isinstance(started, dict) else None
                if new_id:
                    st.session_state["demo_requested_run_id"] = new_id
                else:
                    st.session_state["demo_previous_run_ids"] = [r["run_id"] for r in runs]
                st.rerun()
            except api_client.ApiError as exc:
                st.error({409: "A demo is already queued or running.",
                          429: str(exc)}.get(exc.status, f"Start failed: {exc}"))
        if not runs:
            return
        run_ids = [r["run_id"] for r in runs]
        requested_id = st.session_state.get("demo_requested_run_id")
        previous_ids = st.session_state.get("demo_previous_run_ids")
        if requested_id in run_ids:
            st.session_state["demo_run"] = requested_id
            st.session_state.pop("demo_requested_run_id", None)
            st.session_state.pop("demo_previous_run_ids", None)
        elif previous_ids is not None:
            new_ids = [rid for rid in run_ids if rid not in previous_ids]
            if new_ids:
                st.session_state["demo_run"] = new_ids[0]
                st.session_state.pop("demo_previous_run_ids", None)
        # A previously selected run may no longer exist in the returned list.
        if st.session_state.get("demo_run") not in run_ids:
            st.session_state["demo_run"] = run_ids[0]
        run_id = st.selectbox("Run", run_ids, key="demo_run")
        _run_panel(token, run_id, any(r["run_id"] == run_id and r["status"] in ACTIVE for r in runs))



def _run_panel(token: str, run_id: str, active: bool) -> None:
    @st.fragment(run_every=3 if active else None)
    def panel() -> None:
        try:
            status = api_client.demo_status(token, run_id)
            state = api_client.demo_state(token, run_id)
            points = (
                api_client.demo_metrics(token, run_id)
                if state.get("snapshot")
                else []
            )
        except api_client.ApiError as exc:
            st.error(str(exc))
            return

        current_status = status.get("status", "unknown")

        if active and current_status not in ACTIVE:
            st.rerun(scope="app")

        # Compact validation status
        status_config = {
            "submitting": ("🟡", "Submitting demo request"),
            "queued": ("🟠", "Waiting for Kaggle GPU allocation"),
            "running": ("🔵", "Validation in progress"),
            "complete": ("🟢", "Validation completed"),
            "failed": ("🔴", "Validation failed"),
            "cancelled": ("⚫", "Validation cancelled"),
        }

        icon, description = status_config.get(
            current_status,
            ("⚪", "Status unknown")
        )

        if current_status == "running":
            phase = status.get("phase")
            description = PHASE_TEXT.get(phase, description)

        result = status.get("result")
        if current_status == "complete" and result:
            description = RESULT_TEXT.get(result, description)

        st.markdown(
            f"{icon} **{current_status.upper()}** · {description}"
        )

        if status.get("error_code"):
            st.error(status["error_code"])

        # Validation events
        st.markdown("#### Validation events")
        _events_table(token, run_id)

        # Scaling results
        if (
            state.get("scaling_status") is None
            and not state.get("timeline")
        ):
            return

        _scaling_overview(state)
        _live_metrics(points)
        _timeline(state)
        _summary(state)

    panel()
