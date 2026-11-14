"""Run metrics computed from durable state only (never triggers model work)."""
import math

import config
from storage import db


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def unit_timings(conn, run_id: str | None) -> list[dict]:
    where, args = ("WHERE u.test_run_id=?", (run_id,)) if run_id else ("", ())
    units = conn.execute(
        "SELECT u.unit_id, u.channel, u.status, u.created_at, u.started_at, u.completed_at, u.ingest_ms, "
        "j.attempt FROM intake_units u JOIN intake_jobs j ON j.unit_id=u.unit_id " + where
        + " ORDER BY u.unit_id", args).fetchall()
    steps: dict[tuple[str, str], float] = {}
    step_sql = ("SELECT s.unit_id, s.step_name, SUM(s.finished_at - s.started_at) AS d FROM step_attempts s "
                "JOIN intake_units u ON u.unit_id=s.unit_id WHERE s.status='COMPLETED'"
                + (" AND u.test_run_id=?" if run_id else "") + " GROUP BY s.unit_id, s.step_name")
    for r in conn.execute(step_sql, args):
        steps[(r["unit_id"], r["step_name"])] = r["d"]
    out = []
    for u in units:
        d = dict(u)
        d["queue_wait_s"] = (u["started_at"] - u["created_at"]) if u["started_at"] else None
        d["e2e_s"] = (u["completed_at"] - u["created_at"]) if u["completed_at"] and u["status"] == "COMPLETED" else None
        d["inference_s"] = steps.get((u["unit_id"], "LLM_INFERENCE"))
        d["persistence_s"] = steps.get((u["unit_id"], "SAVE_RESULT"))
        d["handoff_s"] = steps.get((u["unit_id"], "HANDOFF"))
        out.append(d)
    return out


def run_metrics(conn, run: dict | None, now: float | None = None) -> dict:
    now = now or db.now()
    run_id = run["test_run_id"] if run else None
    timings = unit_timings(conn, run_id)
    counts = {k: 0 for k in ("PENDING", "RUNNING", "WAITING_RETRY", "COMPLETED", "FAILED", "MANUAL_REVIEW")}
    for t in timings:
        counts[t["status"]] += 1
    received = len(timings)
    target = run["target_count"] if run else received
    started = run["started_at"] if run else min((t["created_at"] for t in timings), default=now)
    end = (run["finished_at"] if run and run["finished_at"] else now)
    elapsed = max(end - started, 0.0)
    e2e = [t["e2e_s"] for t in timings if t["e2e_s"] is not None]
    completed = counts["COMPLETED"]
    per_min = completed / elapsed * 60 if elapsed > 0 else 0.0
    # Only an estimate, derived from measured throughput; absent until enough units completed.
    eta = None
    if run and run["state"] == "RUNNING" and completed >= 5 and per_min > 0:
        eta = max(target - completed, 0) / (per_min / 60)
    retries = conn.execute(
        "SELECT COALESCE(SUM(MAX(j.attempt-1,0)),0) AS n, COALESCE(SUM(j.attempt>1),0) AS units "
        "FROM intake_jobs j JOIN intake_units u ON u.unit_id=j.unit_id "
        + ("WHERE u.test_run_id=?" if run_id else ""), (run_id,) if run_id else ()).fetchone()
    errors = {r["last_error_code"]: r["n"] for r in conn.execute(
        "SELECT j.last_error_code, COUNT(*) AS n FROM intake_jobs j JOIN intake_units u ON u.unit_id=j.unit_id "
        "WHERE j.status IN ('WAITING_RETRY','FAILED','MANUAL_REVIEW') AND j.last_error_code IS NOT NULL"
        + (" AND u.test_run_id=?" if run_id else "") + " GROUP BY j.last_error_code ORDER BY n DESC",
        (run_id,) if run_id else ())}
    return {
        "error_codes": errors,
        "counts": {"received": received, "completed": completed, "running": counts["RUNNING"],
                   "pending": counts["PENDING"], "waiting_retry": counts["WAITING_RETRY"],
                   "failed": counts["FAILED"], "manual_review": counts["MANUAL_REVIEW"]},
        "target_count": target,
        "completion_pct": round(100 * completed / target, 1) if target else 0.0,
        "completed_per_min": round(per_min, 2),
        "required_per_min": round(target / run["target_seconds"] * 60, 2) if run else None,
        "latency_p50_s": percentile(e2e, 50),
        "latency_p95_s": percentile(e2e, 95),
        "queue_depth": counts["PENDING"] + counts["WAITING_RETRY"],
        "elapsed_seconds": round(elapsed, 1),
        "estimated_remaining_seconds": round(eta, 1) if eta is not None else None,
        "retry_attempts": retries["n"],
        "units_with_retries": retries["units"],
        "breakdown": {
            "ingest_ms_avg": _avg([t["ingest_ms"] for t in timings if t["ingest_ms"] is not None]),
            "queue_wait_s_avg": _avg([t["queue_wait_s"] for t in timings if t["queue_wait_s"] is not None]),
            "queue_wait_s_p95": percentile([t["queue_wait_s"] for t in timings if t["queue_wait_s"] is not None], 95),
            "inference_s_avg": _avg([t["inference_s"] for t in timings if t["inference_s"] is not None]),
            "inference_s_p95": percentile([t["inference_s"] for t in timings if t["inference_s"] is not None], 95),
            "persistence_s_avg": _avg([t["persistence_s"] for t in timings if t["persistence_s"] is not None]),
            "handoff_s_avg": _avg([t["handoff_s"] for t in timings if t["handoff_s"] is not None]),
        },
    }
