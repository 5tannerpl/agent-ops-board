"""Live GPU demo panel: protected start button, run status, the original validation events and the live
Worker / GPU autoscaling view. Everything shown comes from events the Kaggle batch reported."""
import pandas as pd
import streamlit as st
import altair as alt

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


def _llm_observability(token: str, run_id: str, run_start: float | None = None) -> None:
    """Render only persisted trace data; no workflow timing is inferred in the UI."""
    source = st.selectbox("Trace source", ["Kaggle", "External Python Agent (Local)"],
                          key="observability_source")
    if source == "External Python Agent (Local)":
        _external_observability(token)
        return
    try:
        overview = api_client.demo_trace_summary(token, run_id)
        calls = api_client.demo_llm_calls(token, run_id, limit=250,
                                          sort=st.session_state.get("trace_sort", "start"),
                                          unit_id=st.session_state.get("trace_unit_filter") or None,
                                          worker_id=st.session_state.get("trace_worker_filter") or None)
    except api_client.ApiError as exc:
        st.error(str(exc))
        return
    st.markdown("#### Run overview")
    metrics = st.columns(6)
    metrics[0].metric("Model", ", ".join(overview.get("models") or ["N/A"]))
    metrics[1].metric("LLM calls", overview.get("llm_calls", 0))
    metrics[2].metric("Unique units", overview.get("unique_units", 0))
    metrics[3].metric("Input / output tokens",
                      f"{overview.get('input_tokens', 'N/A')} / {overview.get('output_tokens', 'N/A')}")
    metrics[4].metric("LLM avg / P50",
                      f"{na(overview.get('llm_avg_ms'), '{:.0f}', ' ms')} / "
                      f"{na(overview.get('llm_p50_ms'), '{:.0f}', ' ms')}")
    metrics[5].metric("LLM P95", na(overview.get("llm_p95_ms"), "{:.0f}", " ms"))
    st.caption(f"Trace status: **{overview.get('trace_status', 'pending').upper()}** · "
               f"token coverage input {overview.get('input_token_coverage', 'N/A')}, "
               f"output {overview.get('output_token_coverage', 'N/A')} · "
               f"validation spans {overview.get('validation_spans', 0)} · "
               f"task status OK/error {overview.get('task_status_ok', 0)}/"
               f"{overview.get('task_status_error', 0)} · validation status OK/error "
               f"{overview.get('validation_status_ok', 0)}/{overview.get('validation_status_error', 0)} · "
               f"unattributed calls {overview.get('unattributed_calls', 0)}")
    if not calls["items"]:
        st.info("No LLM trace calls have arrived yet. Scaling results remain independent of trace ingestion.")
        return

    items = calls["items"]
    workers = sorted({str(i["attributes"].get("aob.worker_id")) for i in items
                      if i["attributes"].get("aob.worker_id")})
    units = sorted({str(i["attributes"].get("aob.unit_id")) for i in items
                    if i["attributes"].get("aob.unit_id")})
    f1, f2, f3 = st.columns(3)
    f1.selectbox("Sort calls", ["start", "duration"], format_func=lambda x: "Start time" if x == "start" else "Duration",
                 key="trace_sort")
    f2.selectbox("Worker filter", [""] + workers, key="trace_worker_filter")
    f3.selectbox("Unit filter", [""] + units, key="trace_unit_filter")
    items = api_client.demo_llm_calls(token, run_id, limit=250, sort=st.session_state.get("trace_sort", "start"),
                                      unit_id=st.session_state.get("trace_unit_filter") or None,
                                      worker_id=st.session_state.get("trace_worker_filter") or None)["items"]
    if not items:
        st.info("No calls match the selected filters.")
        return
    run_start = run_start or min(i["start_ts"] for i in items)
    timeline = pd.DataFrame([{
        "Call": f"{i['attributes'].get('aob.unit_id') or 'UNATTRIBUTED'} · {i['span_id'][:10]}",
        "Unit ID": i["attributes"].get("aob.unit_id") or "UNATTRIBUTED",
        "Worker": i["attributes"].get("aob.worker_id") or "N/A",
        "Attempt": i["attributes"].get("aob.attempt") or "N/A",
        "Start (s)": i["start_ts"] - run_start,
        "End (s)": i["end_ts"] - run_start,
        "Duration (ms)": i["duration_ms"],
    } for i in items])
    st.markdown("#### Complete LLM calls timeline")
    st.caption("Each bar is one persisted inference Span. X-axis is elapsed time from the first observed call; overlapping bars remain concurrent.")
    chart = alt.Chart(timeline).mark_bar().encode(
        x=alt.X("Start (s):Q", title="Elapsed seconds from run start"),
        x2="End (s):Q", y=alt.Y("Call:N", sort=None, title="Unit ID · Span ID"),
        color=alt.Color("Worker:N", title="Worker"), tooltip=list(timeline.columns),
    ).properties(height=max(180, min(900, len(items) * 22)), width="container")
    st.altair_chart(chart, use_container_width=True)
    st.dataframe(timeline, hide_index=True, width="stretch", height=320)
    labels = [f"{i['attributes'].get('aob.unit_id') or 'UNATTRIBUTED'} · {i['span_id'][:12]}" for i in items]
    selected = st.selectbox("Inspect Call", labels, key="trace_selected_call")
    selected_item = items[labels.index(selected)]
    st.markdown("#### Selected unit execution detail")
    st.json({
        "run_id": run_id, "unit_id": selected_item["attributes"].get("aob.unit_id") or "UNATTRIBUTED",
        "worker_id": selected_item["attributes"].get("aob.worker_id"), "attempt": selected_item["attributes"].get("aob.attempt"),
        "span_id": selected_item["span_id"], "trace_id": selected_item["trace_id"],
        "model": selected_item["attributes"].get("gen_ai.request.model"),
        "start": selected_item["start_utc"], "end": selected_item["end_utc"],
        "duration_ms": selected_item["duration_ms"], "status": selected_item["status"],
        "input_tokens": selected_item["attributes"].get("gen_ai.usage.input_tokens"),
        "output_tokens": selected_item["attributes"].get("gen_ai.usage.output_tokens"),
    })
    unit_id = selected_item["attributes"].get("aob.unit_id")
    if unit_id:
        detail = api_client.demo_unit_trace(token, run_id, unit_id)
        st.markdown("##### Trace waterfall")
        if detail["spans"]:
            waterfall = pd.DataFrame([{
                "Span": s["name"], "Start": s["start_utc"], "Duration (ms)": s["duration_ms"],
                "Attempt": s["attributes"].get("aob.attempt"), "Status": s["status"],
            } for s in detail["spans"]])
            st.dataframe(waterfall, hide_index=True, width="stretch")
        if not detail.get("workflow_available"):
            st.caption("Workflow step timings unavailable: no workflow summary was uploaded with this run.")


def _external_observability(token: str) -> None:
    try:
        runs = api_client.external_runs(token)
    except api_client.ApiError as exc:
        st.error(str(exc))
        return
    if not runs:
        st.info("No OTLP data from the external Python Agent has been received yet.")
        return
    run_id = st.selectbox("External Agent Run ID", [r["run_id"] for r in runs], key="external_run_id")
    try:
        summary = api_client.external_summary(token, run_id)
        units = api_client.external_units(token, run_id)
        workers = api_client.external_workers(token, run_id)
        spans = api_client.external_timeline(token, run_id)
    except api_client.ApiError as exc:
        st.error(str(exc))
        return
    st.caption(f"Trace status: **{summary['trace_status']}** · environment: {summary['environment']}")
    metrics = st.columns(6)
    metrics[0].metric("Run ID", run_id)
    metrics[1].metric("Units", summary["units"])
    metrics[2].metric("Completed / Failed", f"{summary['completed']} / {summary['failed']}")
    metrics[3].metric("Workers", summary["workers"])
    metrics[4].metric("LLM calls", 0)
    metrics[5].metric("Token usage", "N/A")
    st.write({
        "Agent classifications": summary["agent_classifications"],
        "Tool lookups": summary["tool_lookups"],
        "Validations": summary["validations"],
        "Application spans": summary["application_spans"],
    })
    st.markdown("#### Unit-level Timeline")
    st.dataframe(pd.DataFrame(units), hide_index=True, width="stretch")
    st.markdown("#### Span Parent-child Tree")
    st.dataframe(pd.DataFrame([{
        "Span": s["span_name"], "Unit": s["unit_id"], "Worker": s["worker_id"],
        "Span ID": s["span_id"], "Parent span ID": s["parent_span_id"],
        "Start": s["start_time"], "Duration (ms)": s["duration_ms"], "Status": s["status"],
    } for s in spans]), hide_index=True, width="stretch")


def render(token: str) -> None:
    with st.container(border=True):
        st.subheader("Cross-platform agent observability")
        st.caption("Agent Ops Board brings together agents from different platforms so you can monitor them in one "
                   "place. Current sources include Kaggle GPU agents and agents deployed on Azure. This demo checks "
                   "the Kaggle GPU workstation and follows an agent run from start to finish, including model calls, "
                   "tool lookups, validations, timings, and other events behind each task. It runs as a temporary "
                   "Kaggle GPU **batch**; queue time is not guaranteed, and the events and results stay here after "
                   "the batch ends.")
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

        gpu_tab, llm_tab = st.tabs(["GPU & Worker Ops", "LLM Observability"])
        with gpu_tab:
            st.markdown("#### Validation events")
            _events_table(token, run_id)
            if state.get("scaling_status") is not None or state.get("timeline"):
                _scaling_overview(state)
                _live_metrics(points)
                _timeline(state)
                _summary(state)
            else:
                st.info("Scaling telemetry has not arrived yet.")
        with llm_tab:
            _llm_observability(token, run_id, status.get("created_at"))

    panel()
