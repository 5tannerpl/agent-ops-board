"""Authenticated cross-cloud workspace. Session selections are pointers, never grants."""
from datetime import UTC, datetime, time, timedelta
import os

import pandas as pd
import streamlit as st

import api_client as api
import auth_gate
import azure_run
import config
import trace_table
import ui


def cards(**values):
    with st.container(horizontal=True):
        for label, value in values.items():
            st.metric(label, "N/A" if value is None else value, border=True, width=160)


def table(rows, empty):
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.info(empty)


def agent_name(record):
    deployment = record.get("agent_deployment_id") or record["deployment_id"]
    if record["provider"] == "azure" and deployment == os.getenv("AOB_AZURE_RUN_DEPLOYMENT_ID"):
        return "Azure Python Agent"
    return f"{record['provider'].title()} agent · {deployment[:8]}"


def registered(token, role=None):
    return [d for d in api.deployments(token) if (not role or d["runtime_type"] == role) and d["status"] == "registered"]


def deterministic(run):
    return run["provider"] == "azure" and run["workload_version"] == "synthetic-intake-v1"


def selection(token):
    choices = {r["run_id"]: r for r in api.bindings(token, limit=50)["items"]}
    local = st.session_state.get("selected_run")
    if local and local not in choices:
        # Resolve old selections through the same authorised history query as recent ones.
        choices.update({r["run_id"]: r for r in api.bindings(token, search=local)["items"] if r["run_id"] == local})
        if local not in choices:
            st.warning("The previously selected run is no longer available in your authorised history.")
    if not choices:
        st.session_state.pop("selected_run", None)
        st.info("No authorised execution records. Select a run after one has been registered.")
        return None
    ids = list(choices)
    selected = st.selectbox("Selected Run", ids, index=ids.index(local) if local in ids else 0)
    st.session_state.selected_run = selected
    run = choices[selected]
    st.caption(f"{agent_name(run)}  ·  Source: {run['source_id']}  ·  Business outcome: {run['business_status']}")
    return run


def observed(token, run):
    return api.observation(token, run["run_id"], "summary")


def business_badge(status):
    color = {"complete": "green", "running": "blue", "failed": "red", "cancelled": "orange"}.get(status, "gray")
    st.badge("Business outcome: " + status, color=color)


def overview(token, pages):
    inventory = registered(token)
    agents = [d for d in inventory if d["runtime_type"] == "agent"]
    history = api.bindings(token, limit=1)
    latest = next(iter(history["items"]), None)
    try:
        spans = api.received_spans(token)
    except api.ApiError as exc:
        if exc.status != 403:
            raise
        spans = None
        st.warning("Trace totals require an authorised trace-read grant.")
    cards(**{"Registered agents": len(agents), "Run bindings": history["total"],
             "Received spans": spans, "Latest business outcome": latest["business_status"] if latest else None,
             "Inference cost": None})
    st.subheader("Connected infrastructure")
    table([{"Resource": agent_name(d) if d["runtime_type"] == "agent" else d["provider"] + " inference",
            "Evidence": "Registered", "Current health": "Not collected"} for d in inventory]
          + [{"Resource": "OCI control plane and telemetry", "Evidence": "Historically verified in staging",
              "Current health": "Not collected"}]
          + ([] if any(d["provider"].lower() == "runpod" for d in inventory) else
             [{"Resource": "RunPod inference", "Evidence": "Planned integration", "Current health": "N/A"}]),
          "No connected resources.")
    st.subheader("Latest execution")
    if not latest:
        st.info("No authorised run bindings yet.")
        return
    st.text(agent_name(latest))
    st.caption(latest["workload_version"])
    business_badge(latest["business_status"])
    if deterministic(latest) and latest["business_status"] == "complete":
        run = api.stored_run(token, latest["run_id"])
        if (run["dispatch"] or {}).get("state") == "terminal":
            st.progress(1.0, text="100 completed · 0 failed (verified remote response)")
            return
    if spans is None:
        st.caption("Task completion evidence is not available with this account's grants.")
        return
    summary = observed(token, latest)
    complete = summary["completed"] if summary["span_count"] else None
    failed = summary["failed"] if summary["span_count"] else None
    cards(**{"Business outcome": latest["business_status"], "Observed completed": complete, "Observed failed": failed})
    if deterministic(latest) and complete is not None and complete + failed <= 100:
        st.progress((complete + failed) / 100, text=f"{complete + failed} of 100 tasks with observed outcomes")
    else:
        st.caption("Execution progress not collected.")


def execution(token, pages):
    agents = registered(token, "agent")
    eligible = [d for d in agents if d["provider"] == "azure" and d["execution_policy"] == "execute"
                and d["connection_mode"] == "public_https" and {"start", "status"} <= set(d["capabilities"])
                and d["deployment_id"] == os.getenv("AOB_AZURE_RUN_DEPLOYMENT_ID")]
    left, right = st.columns([3, 2])
    with left.container(border=True):
        st.subheader("Execution configuration")
        if eligible:
            st.selectbox("Registered agent", eligible, format_func=agent_name)
            st.caption("Inference resource: none · Fixed deterministic workload · No LLM or GPU allocation")
            st.text("Workload: Synthetic intake v1")
            cards(**{"Tasks": 100, "Concurrency": 4, "Deadline": "120 s", "Estimated cost": None})
        else:
            st.info("No authorised, approved execution target is registered.")
    with right:
        if eligible:
            azure_run.render(token)
    selected = selection(token)
    if selected and selected["run_id"] != st.session_state.get("azure_local_run"):
        run = api.stored_run(token, selected["run_id"])
        dispatch = run["dispatch"] or {}
        st.subheader("Selected execution lifecycle")
        cards(**{"Dispatch": dispatch.get("state"), "Business outcome": run["business_status"],
                 "Dispatch attempts": dispatch.get("attempts"), "Stored telemetry": run["telemetry_status"]})
        st.text(f"Remote Run ID: {run['remote_run_id']}")
        if dispatch.get("last_error_code"):
            st.warning(dispatch["last_error_code"])
        if dispatch and deterministic(selected) and run["business_status"] == "complete":
            st.progress(1.0, text="100 of 100 tasks completed (verified remote response)")
    links(pages, "Agent", "Run History")


def links(pages, *names):
    with st.container(horizontal=True):
        for name in names:
            st.page_link(pages[name], label=f"Open {name}")


def history(token, pages):
    selected_agent = st.session_state.get("history_agent", "")
    if selected_agent:
        st.caption(f"Agent filter: {selected_agent}")
        if st.button("Clear agent filter"):
            del st.session_state.history_agent
            st.rerun()
    a, b, c = st.columns(3)
    search = a.text_input("Search local or remote Run ID")
    provider = b.text_input("Provider", placeholder="All providers")
    status = c.selectbox("Business status", ["", "unknown", "running", "complete", "failed", "cancelled"],
                         format_func=lambda x: x or "All statuses")
    dates = st.date_input("Creation dates (UTC)", value=(), format="YYYY-MM-DD")
    params = dict(search=search, provider=provider, business=status, deployment=selected_agent)
    if dates:
        params["since"] = datetime.combine(dates[0], time.min, UTC).timestamp()
        params["until"] = datetime.combine(dates[-1] + timedelta(days=1), time.min, UTC).timestamp()
    if st.session_state.get("history_filters") != params:
        st.session_state.history_filters = params
        st.session_state.history_page = 1
    page = st.number_input("History page", 1, 501, key="history_page")
    results = api.bindings(token, **params, limit=20, offset=(page - 1) * 20)
    st.caption(f"{results['total']} matching execution records")
    rows = results["items"]
    table([{"Local Run ID": r["run_id"], "Remote Run ID": r["remote_run_id"], "Agent": agent_name(r),
            "Provider": r["provider"], "Business status": r["business_status"], "Stored telemetry": r["telemetry_status"],
            "Created (UTC)": datetime.fromtimestamp(r["created_at"], UTC), "Observed duration": ui.fmt_dur(r["observed_seconds"])}
           for r in rows], "No execution records on this page.")
    if rows:
        ids = [r["run_id"] for r in rows]
        local = st.session_state.get("selected_run")
        choice = st.selectbox("Run to analyse", ids, index=ids.index(local) if local in ids else 0)
        if st.button("Analyse selected run"):
            st.session_state.selected_run = choice
            st.switch_page(pages["Agent"])


def agent(token, pages):
    run = selection(token)
    if not run:
        return
    local = run["run_id"]
    st.text(f"Remote Run ID: {run['remote_run_id']}")
    s = observed(token, run)
    if not s["span_count"]:
        st.info("No spans received for this source and remote run. Trace completeness is unverified.")
        return
    outcomes = s["completed"] + s["failed"]
    cards(**{"Completed tasks": s["completed"], "Failed tasks": s["failed"],
             "Success / known outcomes": f"{s['completed']/outcomes:.1%}" if outcomes else None,
             "Configured concurrency": 4 if deterministic(run) else None,
             "Observed duration": ui.fmt_dur(run["observed_seconds"]), "Received spans": s["span_count"]})
    business_badge(run["business_status"])
    st.caption(f"Stored telemetry: {run['telemetry_status']} · Derived trace: {s['trace_status']} · "
               f"Completion evidence: {s['expectation_origin'] or 'no source manifest; completeness unverified'}")
    outcome, timeline = st.columns([2, 3])
    outcome.subheader("Task execution outcome")
    if s["units"]:
        outcome.vega_lite_chart(pd.DataFrame({"Outcome": ["Complete", "Failed", "Unknown"],
                                              "Tasks": [s["completed"], s["failed"], s["unknown_outcome_units"]]}),
                                {"height": 165, "mark": {"type": "arc", "innerRadius": 43},
                                 "encoding": {"theta": {"field": "Tasks", "type": "quantitative"},
                                              "color": {"field": "Outcome", "type": "nominal", "scale": {
                                                  "domain": ["Complete", "Failed", "Unknown"],
                                                  "range": ["#19875c", "#b13c45", "#8995a3"]}},
                                              "tooltip": [{"field": "Outcome"}, {"field": "Tasks"}]}})
    else:
        outcome.info("No task outcomes observed.")
    units = api.observation(token, local, "units")["items"]
    timeline.subheader("Worker execution timeline")
    windows = [{"Worker": u["worker_id"], "Unit": u["unit_id"],
                "Start": datetime.fromtimestamp(u["start_time"], UTC), "End": datetime.fromtimestamp(u["end_time"], UTC)}
               for u in units if u["worker_id"]]
    if windows:
        timeline.vega_lite_chart(pd.DataFrame(windows), {"height": 165, "mark": "bar", "encoding": {
            "x": {"field": "Start", "type": "temporal"}, "x2": {"field": "End"},
            "y": {"field": "Worker", "type": "nominal"}, "tooltip": [{"field": k} for k in windows[0]]}})
        timeline.caption("First to last observed span per unit; units without an unambiguous worker are omitted.")
    else:
        timeline.info("No observed task intervals with an identified worker.")
    tasks, workers, traces, errors = st.tabs(["Task Details", "Worker Performance", "Distributed Traces", "Errors and Validation"])
    with tasks:
        table([{"Task": u["unit_id"], "Worker": u["worker_id"], "Outcome": u["outcome"],
                "First span (UTC)": datetime.fromtimestamp(u["start_time"], UTC),
                "Last span (UTC)": datetime.fromtimestamp(u["end_time"], UTC), "Spans": u["span_count"]}
               for u in units], "No task evidence.")
    with workers:
        table([{"Worker": w["worker_id"], "Observed tasks": w["unit_count"], "Spans": w["span_count"],
                "Error spans": w["error_count"]} for w in api.observation(token, local, "workers")["items"]],
              "No worker evidence.")
    with traces:
        unit = st.selectbox("Filter spans by unit", [""] + [u["unit_id"] for u in units], format_func=lambda v: v or "All units")
        page = st.number_input("Span page", 1, 101, key=f"spans_{local}_{unit}")
        trace_table.render(api.observation(token, local, "timeline", limit=100, offset=(page-1)*100,
                                           **({"unit": unit} if unit else {}))["items"])
    with errors:
        cards(**{"Failed validations": s["validation_failed"], "Unknown validations": s["validation_unknown"],
                 "Identity conflicts": s["identity_conflicts"], "Unresolved parents": s["unresolved_parent_links"]})


def llm(token, pages):
    run = selection(token)
    measured = api.bound_llm_metrics(token, run["run_id"]) if run else {}
    usage = measured.get("usage") or {}
    cards(**{"Observed LLM calls": measured.get("observed_calls"), "Input tokens": usage.get("input_tokens"),
             "Output tokens": usage.get("output_tokens"), "P50 latency": None, "P95 latency": None, "Inference cost": None})
    if run and deterministic(run) and not measured.get("observed_calls"):
        st.info("No inference telemetry — This workload does not invoke an LLM.")
    st.subheader("Inference resources")
    st.caption("RunPod integration is planned, not verified.")
    st.subheader("Token usage and model performance")
    st.caption("Tokens are shown only when measured. Latency percentiles and cost are not supported.")
    st.subheader("LLM call history")
    st.caption("No call-level inference history is available in this workspace.")


def infrastructure(token, pages):
    run = selection(token)
    st.subheader("Agent runtime")
    cards(**{"Provider": run["provider"] if run else None, "Runtime type": "Agent" if run else None,
             "Region": None, "Health": "Not collected", "Replicas": None})
    st.subheader("Inference infrastructure")
    cards(**{"Inference provider": None, "GPU model": None, "GPU replicas": None})
    st.subheader("Resource utilisation")
    cards(**{"CPU utilisation": None, "Memory utilisation": None, "GPU utilisation": None, "VRAM utilisation": None})
    st.caption("No utilisation samples or scaling events collected for cloud, local or private infrastructure.")
    st.subheader("Infrastructure cost")
    st.caption("Not collected — no provider billing or internal rate data.")


def registry(token, pages):
    agents = registered(token, "agent")
    cards(**{"Registered agents": len(agents), "Execute policy": sum(d["execution_policy"] == "execute" for d in agents),
             "Observe only": sum(d["execution_policy"] == "observe_only" for d in agents)})
    if not agents:
        st.info("No registered agents in your authorised scope.")
    for d in agents:
        with st.container(border=True):
            st.subheader(agent_name(d))
            st.text("Deployment ID: " + d["deployment_id"])
            table([{"Provider": d["provider"], "Runtime": d["runtime_type"], "Region": "N/A",
                    "Ownership": d["ownership"], "Connection": d["connection_mode"].replace("_", " "),
                    "Policy": d["execution_policy"].replace("_", " "),
                    "Capabilities": ", ".join(d["capabilities"]), "Telemetry source": d["source_id"] or "Not configured",
                    "Status": d["status"]}], "")
            if st.button("View execution history", key=f"history_{d['deployment_id']}"):
                st.session_state.history_agent = d["deployment_id"]
                st.switch_page(pages["Run History"])
    st.caption("Future capabilities: connect an existing agent or deploy your own agent.")


def resources(token, pages):
    items = registered(token, "inference")
    cards(**{"Registered inference resources": len(items), "Models connected": None, "GPU endpoints": None})
    st.subheader("Registered resources")
    table([{"Deployment ID": d["deployment_id"], "Provider": d["provider"], "Ownership": d["ownership"],
            "Connectivity": d["connection_mode"], "Capabilities": ", ".join(d["capabilities"]),
            "Health": "Not collected"} for d in items], "No registered inference endpoints in your authorised scope.")
    st.subheader("Planned resource types")
    table([{"Resource": name, "Capability": capability, "Availability": "Planned"} for name, capability in
           [("RunPod Serverless", "Hosted inference"), ("Local inference endpoint", "Existing models and compute"),
            ("Private infrastructure", "Authorised private connectivity")]], "")


GROUPS = {
    "Workspace": [("Overview", overview, "Platform Overview", "Monitor authorised executions, telemetry and infrastructure.")],
    "Agent Operations": [("Execution", execution, "Agent Execution", "Run an approved workload on a registered agent."),
                         ("Run History", history, "Run History", "Explore executions, including those without telemetry.")],
    "Observability": [("Agent", agent, "Agent Observability", "Analyse tasks, workers and distributed traces."),
                      ("LLM", llm, "LLM Observability", "Explore measured inference performance and token usage."),
                      ("Infrastructure", infrastructure, "Infrastructure Observability", "Observe cloud, local and private runtime resources.")],
    "Agent Governance": [("Agent Registry", registry, "Agent Registry", "Inspect agent ownership, connectivity and execution policies."),
                         ("Resource Registry", resources, "Resource Registry", "Explore registered inference and compute resources.")],
}


def render():
    st.set_page_config(page_title="AOB · Agent Ops Board", layout="wide")
    st.html("""<style>
    [data-testid="stSidebar"] {background:#102c49;color:white}
    [data-testid="stSidebar"] :is(p,span,h2,a) {color:#eff6ff}
    [data-testid="stSidebar"] small {color:#c6d8eb}
    [data-testid="stSidebar"] button {background:#224769;color:white}
    [data-testid="stSidebar"] a[aria-current="page"] {background:#235388}
    [data-testid="stMain"] {background:#f7f9fc;color:#162c45}
    [data-testid="stMetric"] {background:white;color:#162c45;border-color:#d8e1eb}
    [data-testid="stMetricValue"] {font-size:1.4rem;white-space:normal}
    [data-testid="stMetricLabel"] p {white-space:normal}
    [data-testid="stSidebar"] [data-testid="stPageLink-NavLink"][aria-current="page"] {background:#235388}
    </style>""")
    st.sidebar.title("AOB")
    st.sidebar.caption("Agent Ops Board · " + os.getenv("AOB_ENVIRONMENT_LABEL", config.AOB_ENV))
    pages = {}

    def page(fn, title, desc):
        def show():
            st.title(title)
            st.caption(desc)
            if not token:
                st.info("Sign in with an authorised account to view workspace data.")
                return
            try:
                fn(token, pages)
            except api.ApiError as exc:
                if exc.status == 413:
                    st.warning("This run exceeds the supported evidence size. No partial totals are shown.")
                else:
                    st.error(f"Workspace data unavailable ({exc.status or 'connection error'}).")
        return show

    groups = {}
    for group, items in GROUPS.items():
        groups[group] = []
        for name, fn, title, desc in items:
            p = st.Page(page(fn, title, desc), title=name, url_path=name.lower().replace(" ", "-"), default=name == "Overview")
            pages[name] = p
            groups[group].append(p)
    current = st.navigation(groups, position="hidden")
    for group, group_pages in groups.items():
        st.sidebar.caption(group.upper())
        for p in group_pages:
            st.sidebar.page_link(p)
    token = auth_gate.require_login()
    current.run()
