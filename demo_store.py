"""SQLite persistence for live-demo runs and the events Kaggle reports.

Three states are tracked independently: ``status`` (our view of the Kaggle batch), ``kaggle_status``
(what the Kaggle CLI last reported) and the events received. A CLI push only proves the submission was
accepted; it never marks a run as having GPU.
"""
import json
import re
import secrets
import sqlite3

import config
from storage import db

ACTIVE = ("submitting", "queued", "running")
TERMINAL = ("complete", "failed", "cancelled")
_RANK = {"submitting": 0, "queued": 1, "running": 2}
RUN_ID_RE = re.compile(r"^run-[0-9a-f]{16}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS demo_runs (
  run_id TEXT PRIMARY KEY, owner_sub TEXT NOT NULL, status TEXT NOT NULL,
  kaggle_status TEXT, kaggle_seen_active INTEGER NOT NULL DEFAULT 0, error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, submitted_at REAL,
  last_synced_at REAL, finished_at REAL);
CREATE INDEX IF NOT EXISTS ix_demo_runs_owner ON demo_runs(owner_sub, created_at);
CREATE INDEX IF NOT EXISTS ix_demo_runs_status ON demo_runs(status);

CREATE TABLE IF NOT EXISTS demo_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, event_id TEXT NOT NULL,
  event_type TEXT NOT NULL, payload TEXT NOT NULL, event_ts REAL, received_at REAL NOT NULL,
  UNIQUE(run_id, event_id));
"""


class Conflict(Exception):
    """Another demo run is already submitting, queued or running."""

    def __init__(self, run_id: str):
        super().__init__(f"Demo run {run_id} is already active")
        self.run_id = run_id


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def connect() -> sqlite3.Connection:
    conn = db.connect(config.DEMO_DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def get_run(conn, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM demo_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(conn, owner_sub: str, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM demo_runs WHERE owner_sub=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                        (owner_sub, limit))
    return [dict(r) for r in rows]


def active_runs(conn) -> list[dict]:
    marks = ",".join("?" * len(ACTIVE))
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM demo_runs WHERE status IN ({marks}) ORDER BY created_at", ACTIVE)]


def create_run(conn, owner_sub: str) -> dict:
    """Atomically enforce one global active run plus per-user cooldown and daily cap."""
    now = db.now()
    with db.tx(conn):
        active = active_runs(conn)
        if active:
            raise Conflict(active[0]["run_id"])
        last = conn.execute("SELECT MAX(created_at) FROM demo_runs WHERE owner_sub=?", (owner_sub,)).fetchone()[0]
        if last is not None and now - last < config.DEMO_MIN_INTERVAL_SECONDS:
            raise RateLimited("Please wait before starting another demo.",
                              config.DEMO_MIN_INTERVAL_SECONDS - (now - last))
        today = conn.execute("SELECT COUNT(*) FROM demo_runs WHERE owner_sub=? AND created_at>?",
                             (owner_sub, now - 86400)).fetchone()[0]
        if today >= config.DEMO_MAX_RUNS_PER_DAY:
            raise RateLimited("Daily demo limit reached.", 3600)
        run_id = f"run-{secrets.token_hex(8)}"
        conn.execute("INSERT INTO demo_runs(run_id, owner_sub, status, created_at, updated_at) "
                     "VALUES(?, ?, 'submitting', ?, ?)", (run_id, owner_sub, now, now))
    return get_run(conn, run_id)


def advance(conn, run_id: str, status: str) -> None:
    """Move an active run forward (never backwards) to submitting < queued < running."""
    now = db.now()
    conn.execute(
        "UPDATE demo_runs SET status=?, updated_at=?, submitted_at=COALESCE(submitted_at, ?) "
        "WHERE run_id=? AND status IN ('submitting','queued','running') "
        "AND (CASE status WHEN 'submitting' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END) < ?",
        (status, now, now if status != "submitting" else None, run_id, _RANK[status]))


def finish(conn, run_id: str, status: str, error: str | None = None) -> None:
    now = db.now()
    conn.execute("UPDATE demo_runs SET status=?, error=?, updated_at=?, finished_at=? "
                 "WHERE run_id=? AND status IN ('submitting','queued','running')",
                 (status, error, now, now, run_id))


def record_sync(conn, run_id: str, kaggle_status: str | None = None, seen_active: bool = False) -> None:
    conn.execute("UPDATE demo_runs SET last_synced_at=?, kaggle_status=COALESCE(?, kaggle_status), "
                 "kaggle_seen_active=MAX(kaggle_seen_active, ?) WHERE run_id=?",
                 (db.now(), kaggle_status, int(seen_active), run_id))


def add_event(conn, run_id: str, event_id: str, event_type: str, payload: dict, event_ts: float | None) -> bool:
    """Insert once per (run_id, event_id); returns False for a duplicate."""
    with db.tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO demo_events(run_id, event_id, event_type, payload, event_ts, received_at) "
            "VALUES(?,?,?,?,?,?)", (run_id, event_id, event_type, json.dumps(payload), event_ts, db.now()))
        if cur.rowcount:
            # Any event proves the notebook is executing on Kaggle.
            conn.execute("UPDATE demo_runs SET kaggle_seen_active=1 WHERE run_id=?", (run_id,))
    if cur.rowcount:
        advance(conn, run_id, "running")
    return bool(cur.rowcount)


def count_events(conn, run_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM demo_events WHERE run_id=?", (run_id,)).fetchone()[0]


def list_events(conn, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict]:
    rows = conn.execute("SELECT id, event_id, event_type, payload, event_ts, received_at FROM demo_events "
                        "WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after_id, limit))
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
