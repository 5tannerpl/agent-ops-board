"""Durable job queue on SQLite: atomic claim, leases with fencing, retry scheduling."""
import json
import random
from dataclasses import dataclass, field

import config
from storage import db


class LeaseLost(Exception):
    """The job lease was reassigned or expired; this worker must stop writing results."""


@dataclass
class Job:
    job_id: int
    unit_id: str
    attempt: int
    lease_version: int
    worker_id: str
    checkpoint: dict = field(default_factory=dict)
    recovered_from: str | None = None


def log_event(conn, worker_id, unit_id, step, event_type, attempt=None, duration_ms=None,
              error_code=None, message=None, details=None) -> None:
    conn.execute(
        "INSERT INTO worker_events(worker_id, unit_id, step_name, event_type, attempt, timestamp, duration_ms, "
        "error_code, message, details) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (worker_id, unit_id, step, event_type, attempt, db.now(), duration_ms, error_code,
         (message or "")[:300] or None, json.dumps(details) if details else None))


def assert_lease(conn, job: Job) -> None:
    """Call inside a write transaction: fencing check on lease owner and version."""
    row = conn.execute(
        "SELECT 1 FROM intake_jobs WHERE job_id=? AND status='RUNNING' AND lease_owner=? AND lease_version=?",
        (job.job_id, job.worker_id, job.lease_version)).fetchone()
    if row is None:
        raise LeaseLost(job.unit_id)


def claim_job(conn, worker_id: str, lease_seconds: float | None = None) -> Job | None:
    lease_seconds = lease_seconds or config.LEASE_SECONDS
    now = db.now()
    with db.tx(conn):
        while True:
            row = conn.execute(
                "SELECT * FROM intake_jobs WHERE status='PENDING' "
                "OR (status='WAITING_RETRY' AND run_after<=?) OR (status='RUNNING' AND lease_until<?) "
                "ORDER BY run_after, job_id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return None
            recovered = row["lease_owner"] if row["status"] == "RUNNING" else None
            if recovered:
                conn.execute(
                    "UPDATE step_attempts SET status='FAILED', finished_at=?, error_code='LEASE_EXPIRED' "
                    "WHERE unit_id=? AND status='RUNNING'", (now, row["unit_id"]))
                log_event(conn, worker_id, row["unit_id"], None, "LEASE_RECOVERED", row["attempt"],
                          error_code="LEASE_EXPIRED",
                          message=f"lease of {recovered} expired; job reassigned")
                if row["attempt"] >= row["max_attempts"]:
                    _finish(conn, row["job_id"], row["unit_id"], "FAILED", "LEASE_EXPIRED",
                            "worker lost and attempts exhausted", now)
                    continue
            attempt = row["attempt"] + 1
            version = row["lease_version"] + 1
            conn.execute(
                "UPDATE intake_jobs SET status='RUNNING', attempt=?, lease_owner=?, lease_until=?, "
                "lease_version=?, updated_at=? WHERE job_id=?",
                (attempt, worker_id, now + lease_seconds, version, now, row["job_id"]))
            conn.execute(
                "UPDATE intake_units SET status='RUNNING', worker_id=?, error_code=NULL, "
                "started_at=COALESCE(started_at, ?) WHERE unit_id=?", (worker_id, now, row["unit_id"]))
            conn.execute(
                "UPDATE workers SET status='BUSY', current_unit_id=?, current_step=NULL, step_started_at=? "
                "WHERE worker_id=?", (row["unit_id"], now, worker_id))
            log_event(conn, worker_id, row["unit_id"], None, "JOB_CLAIMED", attempt,
                      message=f"attempt {attempt} of {row['max_attempts']}")
            return Job(row["job_id"], row["unit_id"], attempt, version, worker_id,
                       json.loads(row["checkpoint"] or "{}"), recovered)


def _finish(conn, job_id, unit_id, status, code, message, now) -> None:
    conn.execute(
        "UPDATE intake_jobs SET status=?, lease_owner=NULL, lease_until=NULL, last_error_code=?, "
        "last_error_message=?, updated_at=? WHERE job_id=?", (status, code, (message or "")[:300], now, job_id))
    conn.execute(
        "UPDATE intake_units SET status=?, error_code=?, completed_at=? WHERE unit_id=?",
        (status, code, now, unit_id))


def save_checkpoint(conn, job: Job, patch: dict) -> None:
    with db.tx(conn):
        assert_lease(conn, job)
        job.checkpoint.update(patch)
        conn.execute("UPDATE intake_jobs SET checkpoint=? WHERE job_id=?",
                     (json.dumps(job.checkpoint), job.job_id))


def complete_job(conn, job: Job) -> None:
    now = db.now()
    with db.tx(conn):
        assert_lease(conn, job)
        _finish(conn, job.job_id, job.unit_id, "COMPLETED", None, None, now)
        conn.execute("UPDATE intake_units SET error_code=NULL WHERE unit_id=?", (job.unit_id,))
        conn.execute("UPDATE intake_jobs SET last_error_code=NULL, last_error_message=NULL WHERE job_id=?",
                     (job.job_id,))
        log_event(conn, job.worker_id, job.unit_id, None, "JOB_COMPLETED", job.attempt)


def retry_delay(attempt: int) -> float:
    delays = config.RETRY_DELAYS
    base = delays[min(attempt - 1, len(delays) - 1)]
    return base + random.uniform(0, max(0.5, base * 0.2))


def fail_or_retry(conn, job: Job, code: str, message: str, max_attempts: int) -> str:
    """Schedule a bounded retry, or end terminally when attempts are exhausted. Returns new status."""
    now = db.now()
    with db.tx(conn):
        assert_lease(conn, job)
        if job.attempt >= max_attempts:
            _finish(conn, job.job_id, job.unit_id, "FAILED", code, message, now)
            log_event(conn, job.worker_id, job.unit_id, None, "JOB_FAILED", job.attempt, error_code=code,
                      message=f"retries exhausted: {message}")
            return "FAILED"
        delay = retry_delay(job.attempt)
        conn.execute(
            "UPDATE intake_jobs SET status='WAITING_RETRY', run_after=?, lease_owner=NULL, lease_until=NULL, "
            "last_error_code=?, last_error_message=?, updated_at=? WHERE job_id=?",
            (now + delay, code, message[:300], now, job.job_id))
        conn.execute("UPDATE intake_units SET status='WAITING_RETRY', error_code=? WHERE unit_id=?",
                     (code, job.unit_id))
        log_event(conn, job.worker_id, job.unit_id, None, "RETRY_SCHEDULED", job.attempt, error_code=code,
                  message=message,
                  details={"retry_at": now + delay, "backoff_seconds": round(delay, 1),
                           "previous_attempt": job.attempt, "next_attempt": job.attempt + 1})
        return "WAITING_RETRY"


def send_to_review(conn, job: Job, code: str, message: str) -> None:
    now = db.now()
    with db.tx(conn):
        assert_lease(conn, job)
        _finish(conn, job.job_id, job.unit_id, "MANUAL_REVIEW", code, message, now)
        log_event(conn, job.worker_id, job.unit_id, None, "MANUAL_REVIEW", job.attempt, error_code=code,
                  message=message)
