"""Read models for the live scaling view, derived only from the events Kaggle reported.

Nothing here is inferred from Kaggle batch status: a batch that merely finished is ``incomplete`` until a
``scaling_demo_completed`` event with measured evidence arrives.
"""
import json

import config
import demo_store as store
from storage import db

SNAPSHOT = "scaling_metrics_snapshot"
STARTED, COMPLETED, FAILED = "scaling_demo_started", "scaling_demo_completed", "scaling_demo_failed"
TIMELINE_TYPES = (
    STARTED, "worker_scaling_requested", "worker_scaling_completed", "gpu_instance_starting", "gpu_instance_ready",
    "gpu_instance_failed", "inference_route_changed", "gpu_instance_draining", "gpu_instance_stopped",
    "scaling_controller_error", COMPLETED, FAILED)
TELEMETRY_DELAY_SECONDS = 30.0
MAX_METRIC_POINTS = 600


def label(event_type: str, d: dict) -> str:
    gpu = f"GPU {d['gpu_id']}" if d.get("gpu_id") is not None else "GPU"
    text = {
        STARTED: lambda: f"Scaling demo started · {d.get('mode')} · {d.get('planned_units')} units",
        "worker_scaling_requested": lambda: f"Workers {d.get('from')} → {d.get('to')} requested ({d.get('reason')})",
        "worker_scaling_completed": lambda: (f"Workers {d.get('from')} → {d.get('to')} {d.get('status')} · "
                                             f"actual {d.get('actual_workers')}"),
        "gpu_instance_starting": lambda: f"{gpu} instance starting ({d.get('reason')})",
        "gpu_instance_ready": lambda: f"{gpu} instance ready" + (
            f" in {d['load_duration_seconds']}s" if d.get("load_duration_seconds") is not None else ""),
        "gpu_instance_failed": lambda: f"{gpu} instance failed: {d.get('error_code')}",
        "inference_route_changed": lambda: f"Router backends: {', '.join(d.get('active_backends') or []) or 'none'}"
                                           f" ({d.get('reason')})",
        "gpu_instance_draining": lambda: f"{gpu} draining · {d.get('inflight_requests')} in flight",
        "gpu_instance_stopped": lambda: f"{gpu} instance stopped · exit {d.get('exit_code')}",
        "scaling_controller_error": lambda: f"Controller error: {d.get('error')}",
        COMPLETED: lambda: f"Scaling demo finished: {str(d.get('status')).upper()}",
        FAILED: lambda: f"Scaling demo failed: {d.get('error_code')} ({d.get('phase')})",
    }.get(event_type)
    return text() if text else event_type


def _last(events: list[dict], event_type: str) -> dict | None:
    return next((e for e in reversed(events) if e["event_type"] == event_type), None)


def status_of(run: dict, events: list[dict]) -> str | None:
    """running | passed | degraded | failed | incomplete, or None when the run never reached the scaling phase."""
    done, failed, started = _last(events, COMPLETED), _last(events, FAILED), _last(events, STARTED)
    if done:
        return done["payload"].get("status") if done["payload"].get("status") in ("passed", "degraded", "failed") \
            else "failed"
    if failed:
        return "failed"
    active = run["status"] in store.ACTIVE
    if started:
        if active:
            return "running"
        return "failed" if run["status"] in ("failed", "cancelled") else "incomplete"
    return "incomplete" if run.get("mode") and run["status"] == "complete" else None


def _scaling_events(conn, run_id: str) -> list[dict]:
    return store.list_events(conn, run_id, 0, config.EVENT_MAX_PER_RUN, TIMELINE_TYPES)


def state(conn, run: dict, now: float | None = None) -> dict:
    now = now or db.now()
    events = _scaling_events(conn, run["run_id"])
    latest = conn.execute("SELECT payload, received_at FROM demo_events WHERE run_id=? AND event_type=? "
                          "ORDER BY id DESC LIMIT 1", (run["run_id"], SNAPSHOT)).fetchone()
    last_any = conn.execute("SELECT MAX(received_at) FROM demo_events WHERE run_id=?", (run["run_id"],)).fetchone()[0]
    snapshot = json.loads(latest["payload"]) if latest else None
    started = _last(events, STARTED)
    status = status_of(run, events)
    age = round(now - last_any, 1) if last_any else None
    active = run["status"] in store.ACTIVE
    completed = _last(events, COMPLETED)
    failure = _last(events, FAILED)
    return {
        "mode": (started["payload"].get("mode") if started else None) or run.get("mode"),
        "units": (started["payload"].get("planned_units") if started else None) or run.get("units"),
        "scaling_status": status, "live": active and status == "running",
        "scaling_state": snapshot.get("scaling_state") if snapshot else None,
        "snapshot": snapshot, "snapshot_received_at": latest["received_at"] if latest else None,
        "seconds_since_last_event": age,
        "telemetry_delayed": bool(active and status == "running" and age is not None
                                  and age > TELEMETRY_DELAY_SECONDS),
        "plan": started["payload"] if started else None,
        "summary": completed["payload"] if completed else None,
        "failure": failure["payload"] if failure else None,
        "timeline": [{"id": e["id"], "received_at": e["received_at"], "event_type": e["event_type"],
                      "label": label(e["event_type"], e["payload"]), "data": e["payload"]} for e in events],
    }


def metrics(conn, run: dict) -> dict:
    """Time series from real snapshots, in receive order; the x axis is seconds since the first snapshot."""
    rows = store.list_events(conn, run["run_id"], 0, config.EVENT_MAX_PER_RUN, (SNAPSHOT,))[-MAX_METRIC_POINTS:]
    t0 = rows[0]["received_at"] if rows else 0
    series = []
    for e in rows:
        d = e["payload"]
        by_gpu, g0, g1 = d.get("requests_by_gpu") or {}, d.get("gpu0") or {}, d.get("gpu1") or {}
        series.append({
            "id": e["id"], "t": round(e["received_at"] - t0, 1), "received_at": e["received_at"],
            "pending": d.get("pending"), "running": d.get("running"), "completed": d.get("completed"),
            "failed": d.get("failed"), "actual_workers": d.get("actual_workers"),
            "desired_workers": d.get("desired_workers"), "gpu_instances_ready": d.get("gpu_instances_ready"),
            "gpu0_requests": by_gpu.get("gpu0"), "gpu1_requests": by_gpu.get("gpu1"),
            "gpu0_utilization_pct": g0.get("utilization_pct"), "gpu1_utilization_pct": g1.get("utilization_pct"),
            "gpu0_memory_mb": g0.get("memory_used_mb"), "gpu1_memory_mb": g1.get("memory_used_mb")})
    return {"points": series}


def summary(conn, run: dict) -> dict:
    events = _scaling_events(conn, run["run_id"])
    done, failed = _last(events, COMPLETED), _last(events, FAILED)
    return {"scaling_status": status_of(run, events), "summary": done["payload"] if done else None,
            "failure": failed["payload"] if failed else None}
