"""Pure scaling decisions and the final verdict; no I/O so every rule is unit-testable."""
import math

from scaling.settings import ScalingSettings

INF = math.inf


def worker_target(s: ScalingSettings, current: int, pending: int, running: int, idle_for_s: float) -> int | None:
    """Next desired worker count, or None to keep it. Scale-out climbs one tier per decision (2 -> 4 -> 8)
    so each step is observable; scale-in goes straight back to the minimum once the queue is idle."""
    if s.mode == "light":
        return s.min_workers if current != s.min_workers else None
    wanted = None
    if pending >= s.scale_out_pending_8:
        wanted = s.max_workers
    elif pending >= s.scale_out_pending_4:
        wanted = s.intermediate_workers
    if wanted is not None and wanted > current:
        ladder = sorted({s.min_workers, s.intermediate_workers, s.max_workers})
        return min(wanted, next(t for t in ladder if t > current))
    if pending == 0 and running == 0 and idle_for_s >= s.idle_before_scale_in_seconds and current > s.min_workers:
        return s.min_workers
    return None


def predicted_drain_seconds(pending: int, rate_per_s: float | None) -> float:
    """Estimate only: pending / observed completion rate. Unknown or zero rate means 'cannot drain yet'."""
    if pending <= 0:
        return 0.0
    if not rate_per_s or rate_per_s <= 0:
        return INF
    return pending / rate_per_s


def gpu1_start_reason(s: ScalingSettings, *, gpu1_state: str, gpu1_available: bool, gpu0_ready: bool,
                      actual_workers: int, backlog_seconds: float, predicted_drain_s: float,
                      cooling_down: bool, attempts: int) -> str | None:
    """Reason text when GPU 1 should be started now, else None."""
    if s.mode != "high_concurrency" or s.max_gpu_instances < 2:
        return None
    if gpu1_state != "STOPPED" or not gpu1_available or not gpu0_ready or cooling_down:
        return None
    if attempts >= s.gpu_max_start_attempts:
        return None
    if actual_workers < s.min_workers_before_gpu_scale or backlog_seconds < s.sustained_backlog_seconds:
        return None
    if predicted_drain_s <= s.predicted_drain_gt_seconds:
        return None
    return (f"backlog sustained {backlog_seconds:.0f}s with {actual_workers} workers; "
            f"estimated drain on one GPU {'unknown' if predicted_drain_s == INF else f'{predicted_drain_s:.0f}s'}")


def gpu1_should_stop(s: ScalingSettings, *, gpu1_state: str, pending: int, running: int, idle_for_s: float) -> bool:
    return (gpu1_state == "READY" and pending == 0 and running == 0
            and idle_for_s >= s.idle_before_scale_in_seconds)


def verdict(s: ScalingSettings, e: dict) -> tuple[str, list[str]]:
    """Return ('passed'|'degraded'|'failed', reasons) from measured evidence only.

    A finished Kaggle batch or a passing model validation is never an input."""
    reasons: list[str] = []
    total, completed = e.get("total", 0), e.get("completed", 0)
    if e.get("timed_out"):
        return "failed", ["RUN_TIMEOUT"]
    if completed == 0:
        return "failed", ["NO_TASK_COMPLETED"]
    if completed + e.get("failed", 0) < total:
        reasons.append("TASKS_NOT_TERMINAL")
    if e.get("failed", 0):
        reasons.append("TASKS_FAILED")
    if e.get("final_workers") != s.min_workers:
        reasons.append("WORKERS_NOT_AT_BASELINE")
    if e.get("telemetry_failed_events"):
        reasons.append("TELEMETRY_DELIVERY_FAILED")
    if s.mode == "high_concurrency":
        levels = set(e.get("confirmed_worker_levels", ()))
        for need in (s.intermediate_workers, s.max_workers):
            if need not in levels:
                reasons.append(f"WORKERS_{need}_NOT_CONFIRMED")
        if e.get("gpu1_unavailable"):
            reasons.append("GPU_UNAVAILABLE")
        elif not e.get("gpu1_ready"):
            reasons.append(e.get("gpu1_error") or "GPU1_NEVER_READY")
        else:
            if not e.get("gpu0_requests"):
                reasons.append("GPU0_NO_INFERENCE_REQUESTS")
            if not e.get("gpu1_requests"):
                reasons.append("GPU1_NO_INFERENCE_REQUESTS")
            if not e.get("gpu1_stopped_cleanly"):
                reasons.append("GPU1_NOT_STOPPED_CLEANLY")
        if e.get("final_gpu_instances") != 1:
            reasons.append("FINAL_GPU_INSTANCES_NOT_1")
    else:
        if e.get("peak_workers", 0) != s.min_workers:
            reasons.append("UNEXPECTED_WORKER_SCALING")
        if e.get("gpu1_ready"):
            reasons.append("UNEXPECTED_GPU1")
    return ("passed" if not reasons else "degraded"), reasons
