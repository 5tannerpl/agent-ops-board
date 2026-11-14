"""Read models for the dashboard API. All functions are cheap SQLite reads."""
import json

import config
from intake.service import load_segments
from loadtest.metrics import run_metrics
from storage import db, files


def _row(r):
    return dict(r) if r is not None else None


def latest_run(conn, run_id: str | None = None) -> dict | None:
    if run_id:
        return _row(conn.execute("SELECT * FROM load_test_runs WHERE test_run_id=?", (run_id,)).fetchone())
    return _row(conn.execute("SELECT * FROM load_test_runs ORDER BY started_at DESC LIMIT 1").fetchone())


def run_view(run: dict, now: float) -> dict:
    end = run["finished_at"] or now
    summary = json.loads(run["summary_json"]) if run["summary_json"] else None
    return {
        "test_run_id": run["test_run_id"], "scenario": run["scenario"], "state": run["state"],
        "target_count": run["target_count"], "completion_deadline_seconds": run["target_seconds"],
        "arrival_window_seconds": run["arrival_window_seconds"], "worker_count": run["worker_count"],
        "started_at": run["started_at"], "finished_at": run["finished_at"],
        "elapsed_seconds": round(end - run["started_at"], 1),
        "deadline_exceeded": (end - run["started_at"]) > run["target_seconds"] and run["state"] == "RUNNING",
        "produced_count": run["produced_count"], "stop_requested": bool(run["stop_requested"]),
        "faults": json.loads(run["config_json"] or "{}").get("faults", {}),
        "acceptance": summary["acceptance"] if summary else None,
        "hardware": summary["hardware"] if summary else None,
        "synthetic": True,
    }


def worker_view(r, now: float, conn=None) -> dict:
    age = now - (r["last_heartbeat"] or 0)
    if r["status"] == "OFFLINE" or age > config.OFFLINE_AFTER:
        state, health = "OFFLINE", "offline"
    elif age > config.UNHEALTHY_AFTER:
        state, health = "UNHEALTHY", "unhealthy"
    else:
        state, health = r["status"], "healthy"
    attempt = lease_until = None
    last_error = None
    if conn is not None and r["current_unit_id"]:
        job = conn.execute("SELECT attempt, lease_until, last_error_code FROM intake_jobs WHERE unit_id=?",
                           (r["current_unit_id"],)).fetchone()
        if job:
            attempt, lease_until, last_error = job["attempt"], job["lease_until"], job["last_error_code"]
    busy = r["current_unit_id"] is not None and state != "OFFLINE"
    return {
        "worker_id": r["worker_id"], "state": state, "health": health, "stored_state": r["status"],
        "current_unit_id": r["current_unit_id"],
        "current_step": r["current_step"] or ("WAITING_FOR_JOB" if state == "IDLE" else None),
        "attempt": attempt,
        "step_elapsed_seconds": round(now - r["step_started_at"], 1) if r["step_started_at"] and busy else None,
        "last_heartbeat": r["last_heartbeat"], "heartbeat_age_seconds": round(age, 1),
        "lease_expiry": lease_until, "last_error_code": last_error,
        "pid": r["pid"], "host": r["host"], "model_name": r["model_name"], "started_at": r["started_at"],
        "backoff_until": r["backoff_until"],
    }


def list_workers(conn) -> list[dict]:
    now = db.now()
    return [worker_view(r, now, conn) for r in conn.execute("SELECT * FROM workers ORDER BY worker_id")]


def get_worker(conn, worker_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
    if r is None:
        return None
    now = db.now()
    view = worker_view(r, now, conn)
    unit_id = r["current_unit_id"]
    is_current = unit_id is not None
    if unit_id is None:
        last = conn.execute("SELECT unit_id FROM worker_events WHERE worker_id=? AND unit_id IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1", (worker_id,)).fetchone()
        unit_id = last["unit_id"] if last else None
    execution = None
    if unit_id:
        job = conn.execute("SELECT attempt, last_error_code, last_error_message FROM intake_jobs WHERE unit_id=?",
                           (unit_id,)).fetchone()
        attempt = job["attempt"]
        steps = unit_steps(conn, unit_id, attempt)
        started = min((s["started_at"] for s in steps if s["started_at"]), default=None)
        last_evt = conn.execute("SELECT MAX(timestamp) FROM worker_events WHERE unit_id=?", (unit_id,)).fetchone()[0]
        execution = {
            "unit_id": unit_id, "is_current": is_current, "step": view["current_step"] if is_current else None,
            "attempt": attempt, "max_attempts": config.MAX_ATTEMPTS, "started_at": started,
            "elapsed_seconds": round(now - started, 1) if started and is_current else None,
            "last_event_at": last_evt, "model_name": r["model_name"], "error_code": job["last_error_code"],
            "error_message": job["last_error_message"], "steps": steps,
        }
    view["execution"] = execution
    return view


def unit_steps(conn, unit_id: str, attempt: int | None = None) -> list[dict]:
    """Per-step status for one attempt (default: latest), always all six steps in order."""
    if attempt is None:
        attempt = conn.execute("SELECT attempt FROM intake_jobs WHERE unit_id=?", (unit_id,)).fetchone()
        attempt = attempt[0] if attempt else 1
    rows = {r["step_name"]: r for r in conn.execute(
        "SELECT * FROM step_attempts WHERE unit_id=? AND attempt=?", (unit_id, attempt))}
    out = []
    for name in config.STEPS:
        r = rows.get(name)
        out.append({
            "step_name": name, "attempt": attempt, "status": r["status"] if r else "NOT_STARTED",
            "worker_id": r["worker_id"] if r else None, "started_at": r["started_at"] if r else None,
            "finished_at": r["finished_at"] if r else None,
            "duration_seconds": round(r["finished_at"] - r["started_at"], 3)
            if r and r["finished_at"] and r["started_at"] else None,
            "error_code": r["error_code"] if r else None,
            "note": r["error_message"] if r else None,
        })
    return out


def worker_events(conn, worker_id: str, limit: int = 100) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM worker_events WHERE worker_id=? ORDER BY id DESC LIMIT ?",
                          (worker_id, limit)):
        d = dict(r)
        d["details"] = json.loads(d["details"]) if d["details"] else None
        out.append(d)
    return out


def list_units(conn, status=None, channel=None, worker_id=None, q=None, intent=None,
               test_run_id=None, created_after=None, created_before=None, page=1, page_size=50) -> dict:
    where, args = [], []
    for col, val in (("u.status", status), ("u.channel", channel), ("u.worker_id", worker_id),
                     ("u.test_run_id", test_run_id)):
        if val:
            where.append(f"{col}=?")
            args.append(val)
    if q:
        where.append("u.unit_id LIKE ? ESCAPE '\\'")
        args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if intent:
        where.append("EXISTS (SELECT 1 FROM intent_evidence e WHERE e.unit_id=u.unit_id AND e.label=?)")
        args.append(intent)
    if created_after is not None:
        where.append("u.created_at>=?")
        args.append(created_after)
    if created_before is not None:
        where.append("u.created_at<=?")
        args.append(created_before)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM intake_units u {clause}", args).fetchone()[0]
    now = db.now()
    rows = conn.execute(
        f"SELECT u.*, j.attempt FROM intake_units u JOIN intake_jobs j ON j.unit_id=u.unit_id {clause} "
        "ORDER BY u.created_at DESC, u.unit_id DESC LIMIT ? OFFSET ?", [*args, page_size, (page - 1) * page_size])
    items = [{
        "unit_id": r["unit_id"], "channel": r["channel"], "preview": r["preview"], "status": r["status"],
        "worker_id": r["worker_id"], "current_step": r["current_step"], "attempt": r["attempt"],
        "created_at": r["created_at"], "test_run_id": r["test_run_id"],
        "duration_seconds": round((r["completed_at"] or now) - r["created_at"], 1),
    } for r in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_unit(conn, unit_id: str) -> dict | None:
    u = conn.execute("SELECT u.*, j.attempt, j.max_attempts, j.last_error_code, j.last_error_message "
                     "FROM intake_units u JOIN intake_jobs j ON j.unit_id=u.unit_id WHERE u.unit_id=?",
                     (unit_id,)).fetchone()
    if u is None:
        return None
    try:
        segments, source_error = load_segments(conn, unit_id), None
    except files.CorruptInput as exc:
        segments, source_error = [], str(exc)
    msg_ids = [r[0] for r in conn.execute(
        "SELECT message_id FROM source_messages WHERE conversation_id=? ORDER BY seq", (u["conversation_id"],))]
    res = conn.execute("SELECT * FROM intake_results WHERE unit_id=? AND schema_version=?",
                       (unit_id, config.SCHEMA_VERSION)).fetchone()
    result = None
    if res:
        result = json.loads(res["result_json"])
        result["model_version"], result["created_at"] = res["model_version"], res["created_at"]
    ho = handoff_view(conn, unit_id)
    return {
        "unit_id": unit_id, "channel": u["channel"], "conversation_id": u["conversation_id"],
        "source_message_ids": msg_ids, "received_at": u["created_at"], "status": u["status"],
        "assigned_worker": u["worker_id"], "current_step": u["current_step"], "attempt": u["attempt"],
        "max_attempts": u["max_attempts"], "error_code": u["last_error_code"] if u["status"] != "COMPLETED" else None,
        "error_message": u["last_error_message"] if u["status"] != "COMPLETED" else None,
        "test_run_id": u["test_run_id"], "started_at": u["started_at"], "completed_at": u["completed_at"],
        "segments": segments, "source_error": source_error,
        "attachments": [{"attachment_id": a["attachment_id"], "filename": a["filename"],
                         "size_bytes": a["size_bytes"]}
                        for a in conn.execute("SELECT * FROM attachments WHERE unit_id=?", (unit_id,))],
        "intents": result["intents"] if result else [],
        "entities": result["entities"] if result else [],
        "validation": result["validation"] if result else None,
        "result_meta": {"model_version": result["model_version"], "schema_version": result["schema_version"],
                        "created_at": result["created_at"]} if result else None,
        "handoff": ho,
    }


def handoff_view(conn, unit_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM handoff_outbox WHERE unit_id=? ORDER BY created_at DESC LIMIT 1",
                     (unit_id,)).fetchone()
    if r is None:
        return None
    payload = json.loads(r["payload"])
    inbox = conn.execute("SELECT received_at FROM orchestrator_inbox WHERE event_id=?", (r["event_id"],)).fetchone()
    return {"handoff_event_id": r["event_id"], "status": r["status"], "targets": payload["targets"],
            "attempts": r["attempts"], "last_error": r["last_error"], "created_at": r["created_at"],
            "delivered_at": r["delivered_at"], "orchestrator_received_at": inbox["received_at"] if inbox else None,
            "note": "Mock orchestrator: no account updates or external replies are performed."}
