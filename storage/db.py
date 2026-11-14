"""SQLite access: WAL mode, short transactions, schema and id allocation."""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS id_counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS source_messages (
  message_id TEXT PRIMARY KEY, channel TEXT NOT NULL, conversation_id TEXT NOT NULL,
  raw_path TEXT NOT NULL, received_at REAL NOT NULL, seq INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON source_messages(conversation_id, seq);

CREATE TABLE IF NOT EXISTS intake_units (
  unit_id TEXT PRIMARY KEY, channel TEXT NOT NULL, conversation_id TEXT NOT NULL,
  preview TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, worker_id TEXT, current_step TEXT,
  test_run_id TEXT, fault TEXT, ingest_ms REAL, created_at REAL NOT NULL,
  started_at REAL, completed_at REAL, error_code TEXT);
CREATE INDEX IF NOT EXISTS ix_units_status ON intake_units(status);
CREATE INDEX IF NOT EXISTS ix_units_created ON intake_units(created_at);
CREATE INDEX IF NOT EXISTS ix_units_run ON intake_units(test_run_id);

CREATE TABLE IF NOT EXISTS intake_jobs (
  job_id INTEGER PRIMARY KEY AUTOINCREMENT, unit_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL, run_after REAL NOT NULL,
  lease_owner TEXT, lease_until REAL, lease_version INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT, last_error_message TEXT, checkpoint TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON intake_jobs(status, run_after);

CREATE TABLE IF NOT EXISTS workers (
  worker_id TEXT PRIMARY KEY, status TEXT NOT NULL, current_unit_id TEXT, current_step TEXT,
  step_started_at REAL, pid INTEGER, host TEXT, model_name TEXT, started_at REAL,
  last_heartbeat REAL, backoff_until REAL);

CREATE TABLE IF NOT EXISTS step_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, unit_id TEXT NOT NULL, step_name TEXT NOT NULL,
  attempt INTEGER NOT NULL, status TEXT NOT NULL, worker_id TEXT, started_at REAL, finished_at REAL,
  error_code TEXT, error_message TEXT, UNIQUE(unit_id, step_name, attempt));

CREATE TABLE IF NOT EXISTS intake_results (
  unit_id TEXT NOT NULL, schema_version TEXT NOT NULL, model_version TEXT NOT NULL,
  result_json TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (unit_id, schema_version));

CREATE TABLE IF NOT EXISTS intent_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, unit_id TEXT NOT NULL, label TEXT NOT NULL,
  source_message_id TEXT NOT NULL, start_offset INTEGER, end_offset INTEGER,
  quote TEXT NOT NULL, verified INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_evidence_unit ON intent_evidence(unit_id);
CREATE INDEX IF NOT EXISTS ix_evidence_label ON intent_evidence(label);

CREATE TABLE IF NOT EXISTS handoff_outbox (
  event_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at REAL NOT NULL, delivered_at REAL);
CREATE INDEX IF NOT EXISTS ix_outbox_unit ON handoff_outbox(unit_id);

-- Mock downstream orchestrator inbox; event_id primary key makes delivery idempotent.
CREATE TABLE IF NOT EXISTS orchestrator_inbox (
  event_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, payload TEXT NOT NULL, received_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS worker_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT, unit_id TEXT, step_name TEXT,
  event_type TEXT NOT NULL, attempt INTEGER, timestamp REAL NOT NULL, duration_ms REAL,
  error_code TEXT, message TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS ix_wevents_worker ON worker_events(worker_id, id);
CREATE INDEX IF NOT EXISTS ix_wevents_unit ON worker_events(unit_id, id);

CREATE TABLE IF NOT EXISTS attachments (
  attachment_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, filename TEXT NOT NULL,
  path TEXT NOT NULL, size_bytes INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_att_unit ON attachments(unit_id);

CREATE TABLE IF NOT EXISTS load_test_runs (
  test_run_id TEXT PRIMARY KEY, scenario TEXT NOT NULL, target_count INTEGER NOT NULL,
  target_seconds REAL NOT NULL, arrival_window_seconds REAL NOT NULL, worker_count INTEGER NOT NULL,
  started_at REAL NOT NULL, finished_at REAL, state TEXT NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0,
  produced_count INTEGER NOT NULL DEFAULT 0, producer_done INTEGER NOT NULL DEFAULT 0,
  config_json TEXT, summary_json TEXT);
"""


def now() -> float:
    return time.time()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: transactions are managed explicitly through tx().
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection):
    """Short write transaction; BEGIN IMMEDIATE avoids upgrade deadlocks."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_id(conn: sqlite3.Connection, name: str, prefix: str, width: int = 4) -> str:
    """Atomically allocate a sequential id; call inside tx()."""
    conn.execute(
        "INSERT INTO id_counters(name, value) VALUES(?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        (name,),
    )
    value = conn.execute("SELECT value FROM id_counters WHERE name=?", (name,)).fetchone()[0]
    return f"{prefix}-{value:0{width}d}"


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
