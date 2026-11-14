"""Shared Streamlit helpers: formatting, status icons, page chrome, navigation."""
import datetime as dt

import streamlit as st

import api_client

STATUS_ICON = {
    "COMPLETED": "🟢", "RUNNING": "🔵", "PENDING": "⚪", "WAITING_RETRY": "🟠", "FAILED": "🔴",
    "MANUAL_REVIEW": "🟣", "SKIPPED": "⏭️", "NOT_STARTED": "⚪", "DELIVERED": "🟢",
    "IDLE": "⚪", "BUSY": "🔵", "BACKOFF": "🟠", "UNHEALTHY": "🟡", "OFFLINE": "⚫",
}


def badge(status: str | None) -> str:
    return f"{STATUS_ICON.get(status or '', '•')} {status or '—'}"


def fmt_ts(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"


def fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 120:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def fmt_age(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds:.0f}s ago"


def api_guard(fn, *args, **kwargs):
    """Call the API; show a friendly error and stop rendering the page section on failure."""
    try:
        return fn(*args, **kwargs)
    except api_client.ApiError as exc:
        st.error(str(exc))
        st.stop()


def page_header(title: str) -> None:
    st.set_page_config(page_title=f"Agent Ops Board — {title}", layout="wide", page_icon="🛰️")
    st.title(title)
    st.caption("🧪 **Synthetic Demo** — synthetic, non-confidential content. No account updates or external replies.")


def open_unit(unit_id: str) -> None:
    st.session_state["unit_id"] = unit_id
    st.switch_page("pages/intake_unit_detail.py", query_params={"unit_id": unit_id})


def open_worker(worker_id: str) -> None:
    st.session_state["worker_id"] = worker_id
    st.switch_page("pages/worker_monitor.py", query_params={"worker_id": worker_id})


def back_to_dashboard() -> None:
    if st.button("← Back to Dashboard"):
        st.switch_page("app.py")


def model_banner(model: dict) -> None:
    line = f"Model: **{model['name']}** via **{model['backend']}**"
    if model.get("server"):
        line += f" ({model['server']})"
    if model.get("simulated"):
        st.warning(f"{line} — **SIMULATED rule-based stand-in, not LLM inference.** "
                   "Timings are not valid throughput evidence.")
    else:
        st.caption(line)


def stat(col, label: str, value) -> None:
    """Compact label/value tile that does not truncate long values (unlike st.metric)."""
    col.caption(label)
    col.markdown(f"**{value}**")
