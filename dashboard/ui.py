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


AREAS = {
    "dashboard": ("#6366f1", "99,102,241", "Dashboard", "Live load-test progress and KPIs"),
    "workers": ("#10b981", "16,185,129", "Workers", "Health, current unit and step"),
    "units": ("#f59e0b", "245,158,11", "Intake units", "Search, filter and open a unit"),
}


def inject_area_css() -> None:
    """Colour-coded section cards: one accent per area (dashboard / workers / units)."""
    rules = []
    for key, (hex_, rgb, _, _) in AREAS.items():
        rules.append(
            f".st-key-area_{key} {{border-radius:16px; padding:1rem 1.4rem 1.2rem; margin:1.1rem 0;"
            f"border:1px solid rgba({rgb},.28); border-left:7px solid {hex_};"
            f"background:linear-gradient(135deg, rgba({rgb},.10) 0%, rgba({rgb},.02) 55%, transparent 100%);"
            f"box-shadow:0 2px 10px rgba({rgb},.12);}}"
            f".area-{key} .area-title {{color:{hex_};}} .area-{key} .area-dot {{background:{hex_};"
            f"box-shadow:0 0 0 4px rgba({rgb},.22);}}")
    rules.append(
        ".area-head {display:flex; align-items:center; gap:.7rem; margin:.1rem 0 .8rem;}"
        ".area-dot {width:12px; height:12px; border-radius:50%;}"
        ".area-title {font-size:1.45rem; font-weight:700; letter-spacing:-.01em;}"
        ".area-sub {opacity:.65; font-size:.9rem; margin-left:.2rem;}")
    st.markdown(f"<style>{''.join(rules)}</style>", unsafe_allow_html=True)


def area_header(key: str) -> None:
    _, _, title, sub = AREAS[key]
    st.markdown(f'<div class="area-head area-{key}"><span class="area-dot"></span>'
                f'<span class="area-title">{title}</span><span class="area-sub">{sub}</span></div>',
                unsafe_allow_html=True)
