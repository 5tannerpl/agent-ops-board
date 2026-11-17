"""Read models for the live scaling view, derived only from the events Kaggle reported.

Nothing here is inferred from Kaggle batch status: a batch that merely finished is ``incomplete`` until a
``scaling_demo_completed`` event with measured evidence arrives.
"""
import json
import math

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
_SAFE_SUMMARY_FIELDS = (
    "status", "completed", "total", "failed", "retries", "p50_seconds", "p95_seconds", "throughput_per_min",
    "peak_workers", "final_workers", "confirmed_worker_levels", "peak_gpu_instances", "final_gpu_instances",
    "gpu0_units_completed", "gpu1_units_completed", "unattributed_units", "gpu1_load_seconds",
    "gpu1_stopped_cleanly", "scale_out_seconds", "scale_in_seconds", "telemetry_failed_events", "reasons"
)
_GPU_KEYS = ("gpu0", "gpu1")
_ALLOWED_MODES = {"light", "high_concurrency"}
_ALLOWED_FAILURE_CODES = {
    "GPU_UNAVAILABLE", "ROUTER_TIMEOUT", "SCALING_TIMEOUT", "LOAD_TEST_TIMEOUT", "ENVIRONMENT_ERROR",
    "SCENARIO_ERROR", "WORKER_SCALING_FAILED", "SCALING_FAILED"
}
_ALLOWED_FAILURE_PHASES = {"environment", "setup", "gpu0", "router", "workers", "scenario", "load", "scale_in",
                           "summary"}
_ALLOWED_REASON_CODES = {
    "TASKS_NOT_TERMINAL", "TASKS_FAILED", "WORKERS_NOT_AT_BASELINE", "TELEMETRY_DELIVERY_FAILED", "GPU_UNAVAILABLE",
    "GPU1_NEVER_READY", "GPU0_NO_INFERENCE_REQUESTS", "GPU1_NO_INFERENCE_REQUESTS", "GPU1_NOT_STOPPED_CLEANLY",
    "FINAL_GPU_INSTANCES_NOT_1", "UNEXPECTED_WORKER_SCALING", "UNEXPECTED_GPU1"
}
_ALLOWED_SUMMARY_STATUSES = {"passed", "degraded", "failed"}
_ALLOWED_WORKER_EVENT_STATUSES = {"applied", "confirmed", "skipped", "failed"}
_SAFE_FAILURE_MESSAGES = {
    "GPU_UNAVAILABLE": "GPU capacity was unavailable during the scaling run.",
    "ROUTER_TIMEOUT": "The inference router did not stabilize within the allowed time.",
    "SCALING_TIMEOUT": "Worker/GPU scaling did not complete in time.",
    "LOAD_TEST_TIMEOUT": "Synthetic workload did not finish in time.",
    "ENVIRONMENT_ERROR": "The runtime environment could not be prepared.",
    "SCENARIO_ERROR": "The scaling scenario failed to complete.",
    "WORKER_SCALING_FAILED": "Worker scaling failed.",
    "SCALING_FAILED": "Scaling execution failed.",
}


def label(event_type: str, d: dict) -> str:
    d = _payload_dict(d)
    gpu_id = _safe_gpu_label_id(d.get("gpu_id"))
    gpu = f"GPU {gpu_id}" if gpu_id is not None else "GPU"
    error_code = _safe_failure_code(d.get("error_code") or d.get("error"))
    ready_seconds = _safe_non_negative_number(d.get("load_duration_seconds"))
    inflight = _safe_non_negative_int(d.get("inflight_requests"))
    exit_code = _safe_non_negative_int(d.get("exit_code"))
    worker_status = _safe_worker_event_status(d.get("status"))
    text = {
        STARTED: lambda: f"Scaling demo started · {_safe_mode(d.get('mode')) or 'unknown'} · "
                         f"{_safe_non_negative_int(d.get('planned_units')) or '?'} units",
        "worker_scaling_requested": lambda: f"Workers {_safe_non_negative_int(d.get('from')) or '?'} → "
                                            f"{_safe_non_negative_int(d.get('to')) or '?'} requested",
        "worker_scaling_completed": lambda: (
            f"Workers {_safe_non_negative_int(d.get('from')) or '?'} → {_safe_non_negative_int(d.get('to')) or '?'} "
            f"{worker_status} · actual {_safe_non_negative_int(d.get('actual_workers')) or '?'}"
        ),
        "gpu_instance_starting": lambda: f"{gpu} instance starting",
        "gpu_instance_ready": lambda: f"{gpu} instance ready" + (
            f" in {ready_seconds}s" if ready_seconds is not None else ""),
        "gpu_instance_failed": lambda: f"{gpu} instance failed: {error_code}",
        "inference_route_changed": lambda: f"Router backends: "
                                           f"{', '.join(_safe_backends(d.get('active_backends'))) or 'none'}",
        "gpu_instance_draining": lambda: f"{gpu} draining · {inflight if inflight is not None else '?'} in flight",
        "gpu_instance_stopped": lambda: f"{gpu} instance stopped · exit {exit_code if exit_code is not None else '?'}",
        "scaling_controller_error": lambda: f"Controller error: {error_code}",
        COMPLETED: lambda: f"Scaling demo finished: {_safe_summary_status(d.get('status')).upper()}",
        FAILED: lambda: f"Scaling demo failed: {_safe_failure_code(d.get('error_code'))} "
                        f"({_safe_failure_phase(d.get('phase'))})",
    }.get(event_type)
    return text() if text else event_type


def _last(events: list[dict], event_type: str) -> dict | None:
    return next((e for e in reversed(events) if e["event_type"] == event_type), None)


def _safe_gpu(gpu: dict | None) -> dict | None:
    gpu = _payload_dict(gpu)
    if not gpu:
        return None
    out = {}
    state = gpu.get("state")
    if isinstance(state, str):
        out["state"] = state[:32]
    mem = _safe_non_negative_number(gpu.get("memory_used_mb"))
    if mem is not None:
        out["memory_used_mb"] = mem
    util = _safe_non_negative_number(gpu.get("utilization_pct"), upper=100)
    if util is not None:
        out["utilization_pct"] = util
    return out or None


def _safe_snapshot(snapshot: dict | None) -> dict | None:
    snapshot = _payload_dict(snapshot)
    if not snapshot:
        return None
    clean = {}
    for key in ("pending", "running", "completed", "failed", "total", "actual_workers", "desired_workers",
                "gpu_instances_ready", "unattributed_units"):
        value = _safe_non_negative_int(snapshot.get(key))
        if value is not None:
            clean[key] = value
    if isinstance(snapshot.get("scaling_state"), str):
        clean["scaling_state"] = snapshot.get("scaling_state")[:32]
    for map_key in ("requests_by_gpu", "inflight_by_gpu", "units_by_gpu"):
        safe_map = _safe_gpu_map(snapshot.get(map_key))
        if safe_map:
            clean[map_key] = safe_map
    clean["gpu0"] = _safe_gpu(snapshot.get("gpu0"))
    clean["gpu1"] = _safe_gpu(snapshot.get("gpu1"))
    return clean


def _safe_fields(data: dict | None, allowed: tuple[str, ...]) -> dict | None:
    if not isinstance(data, dict):
        return None
    return {k: data.get(k) for k in allowed if k in data}


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


def _payload_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_mode(value) -> str | None:
    return value if value in _ALLOWED_MODES else None


def _safe_non_negative_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer():
        cast = int(value)
        return cast if cast >= 0 else None
    return None


def _safe_non_negative_number(value, upper: float | None = None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    cast = float(value)
    if not math.isfinite(cast) or cast < 0:
        return None
    if upper is not None and cast > upper:
        return None
    return cast if isinstance(value, float) else int(value)


def _safe_gpu_label_id(value):
    if value in (0, "0", "gpu0"):
        return 0
    if value in (1, "1", "gpu1"):
        return 1
    return None


def _safe_gpu_map(value) -> dict:
    data = _payload_dict(value)
    out = {}
    for key in _GPU_KEYS:
        num = _safe_non_negative_int(data.get(key))
        if num is not None:
            out[key] = num
    return out


def _safe_backends(value) -> list[str]:
    if not isinstance(value, list):
        return []
    safe = []
    for item in value:
        if item in _GPU_KEYS and item not in safe:
            safe.append(item)
    return safe


def _safe_summary_status(value) -> str:
    if isinstance(value, str) and value in _ALLOWED_SUMMARY_STATUSES:
        return value
    return "failed"


def _safe_worker_event_status(value) -> str:
    if isinstance(value, str) and value in _ALLOWED_WORKER_EVENT_STATUSES:
        return value
    return "updated"


def _safe_reasons(value) -> list[str]:
    if not isinstance(value, list):
        return []
    safe = []
    for reason in value:
        if isinstance(reason, str) and (
                reason in _ALLOWED_REASON_CODES or
                (reason.startswith("WORKERS_") and reason.endswith("_NOT_CONFIRMED"))):
            safe.append(reason)
    return safe


def _safe_failure_code(value) -> str:
    if isinstance(value, str) and value in _ALLOWED_FAILURE_CODES:
        return value
    return "SCALING_FAILED"


def _safe_failure_phase(value) -> str:
    if isinstance(value, str) and value in _ALLOWED_FAILURE_PHASES:
        return value
    return "summary"


def _safe_plan(data: dict | None) -> dict | None:
    d = _payload_dict(data)
    if not d:
        return None
    mode = _safe_mode(d.get("mode"))
    units = _safe_non_negative_int(d.get("planned_units"))
    out = {}
    if mode is not None:
        out["mode"] = mode
    if units is not None:
        out["planned_units"] = units
    return out or None


def _safe_summary(data: dict | None) -> dict | None:
    d = _safe_fields(data, _SAFE_SUMMARY_FIELDS)
    if not d:
        return None
    clean = {}
    for key in ("completed", "total", "failed", "retries", "peak_workers", "final_workers", "peak_gpu_instances",
                "final_gpu_instances", "gpu0_units_completed", "gpu1_units_completed", "unattributed_units",
                "telemetry_failed_events"):
        value = _safe_non_negative_int(d.get(key))
        if value is not None:
            clean[key] = value
    for key in ("p50_seconds", "p95_seconds", "throughput_per_min", "gpu1_load_seconds", "scale_out_seconds",
                "scale_in_seconds"):
        value = _safe_non_negative_number(d.get(key))
        if value is not None:
            clean[key] = value
    clean["status"] = _safe_summary_status(d.get("status"))
    if isinstance(d.get("gpu1_stopped_cleanly"), bool):
        clean["gpu1_stopped_cleanly"] = d["gpu1_stopped_cleanly"]
    if isinstance(d.get("confirmed_worker_levels"), list):
        levels = [_safe_non_negative_int(v) for v in d["confirmed_worker_levels"]]
        clean["confirmed_worker_levels"] = [v for v in levels if v is not None]
    reasons = _safe_reasons(d.get("reasons"))
    if reasons:
        clean["reasons"] = reasons
    return clean or None


def _safe_failure(data: dict | None) -> dict | None:
    d = _payload_dict(data)
    if not d:
        return None
    code = _safe_failure_code(d.get("error_code"))
    phase = _safe_failure_phase(d.get("phase"))
    return {"error_code": code, "phase": phase, "message": _SAFE_FAILURE_MESSAGES[code]}


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
    safe_snapshot = _safe_snapshot(snapshot)
    safe_plan = _safe_plan(started["payload"] if started else None)
    return {
        "mode": (safe_plan or {}).get("mode") or run.get("mode"),
        "units": (safe_plan or {}).get("planned_units") or run.get("units"),
        "scaling_status": status, "live": active and status == "running",
        "scaling_state": safe_snapshot.get("scaling_state") if safe_snapshot else None,
        "snapshot": safe_snapshot, "snapshot_received_at": latest["received_at"] if latest else None,
        "seconds_since_last_event": age,
        "telemetry_delayed": bool(active and status == "running" and age is not None
                                  and age > TELEMETRY_DELAY_SECONDS),
        "plan": safe_plan,
        "summary": _safe_summary(completed["payload"]) if completed else None,
        "failure": _safe_failure(failure["payload"]) if failure else None,
        "timeline": [{"id": e["id"], "received_at": e["received_at"], "event_type": e["event_type"],
                      "label": label(e["event_type"], _payload_dict(e["payload"]))} for e in events],
    }


def metrics(conn, run: dict) -> dict:
    """Time series from real snapshots, in receive order; the x axis is seconds since the first snapshot."""
    rows = store.list_events(conn, run["run_id"], 0, config.EVENT_MAX_PER_RUN, (SNAPSHOT,))[-MAX_METRIC_POINTS:]
    t0 = rows[0]["received_at"] if rows else 0
    series = []
    for e in rows:
        d = _safe_snapshot(e["payload"]) or {}
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
    return {"scaling_status": status_of(run, events),
            "summary": _safe_summary(done["payload"]) if done else None,
            "failure": _safe_failure(failed["payload"]) if failed else None}
