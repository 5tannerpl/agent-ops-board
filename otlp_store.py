"""Persistence and query helpers for traces received from external agents."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from storage import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_agent_spans (
  source_id TEXT NOT NULL, environment TEXT NOT NULL, run_id TEXT NOT NULL,
  unit_id TEXT, worker_id TEXT, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
  parent_span_id TEXT, span_name TEXT NOT NULL, start_time REAL NOT NULL,
  end_time REAL NOT NULL, duration_ms REAL NOT NULL, status TEXT NOT NULL,
  validation_result TEXT NOT NULL, attributes TEXT NOT NULL,
  PRIMARY KEY (source_id, trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS ix_external_run ON external_agent_spans(source_id, run_id, start_time);
CREATE INDEX IF NOT EXISTS ix_external_unit ON external_agent_spans(source_id, run_id, unit_id);
CREATE TABLE IF NOT EXISTS external_agent_completions (
  source_id TEXT NOT NULL, run_id TEXT NOT NULL, expected_spans INTEGER NOT NULL,
  expected_units INTEGER NOT NULL, completed_at REAL NOT NULL,
  status TEXT NOT NULL, PRIMARY KEY (source_id, run_id)
);
"""


def connect(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def insert_spans(conn: sqlite3.Connection, spans: list[dict]) -> tuple[int, int]:
    accepted = duplicate = 0
    with db.tx(conn):
        for span in spans:
            if span["parent_span_id"]:
                parent = conn.execute(
                    "SELECT 1 FROM external_agent_spans WHERE source_id=? AND trace_id=? AND span_id=?",
                    (span["source_id"], span["trace_id"], span["parent_span_id"]),
                ).fetchone()
                if not parent and not any(
                    other["source_id"] == span["source_id"] and other["trace_id"] == span["trace_id"]
                    and other["span_id"] == span["parent_span_id"] for other in spans
                ):
                    span["validation_result"] = "missing_parent"
            cur = conn.execute(
                "INSERT OR IGNORE INTO external_agent_spans("
                "source_id,environment,run_id,unit_id,worker_id,trace_id,span_id,parent_span_id,"
                "span_name,start_time,end_time,duration_ms,status,validation_result,attributes)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (span["source_id"], span["environment"], span["run_id"], span["unit_id"],
                 span["worker_id"], span["trace_id"], span["span_id"], span["parent_span_id"],
                 span["span_name"], span["start_time"], span["end_time"], span["duration_ms"],
                 span["status"], span["validation_result"], json.dumps(span["attributes"], separators=(",", ":"))),
            )
            if cur.rowcount:
                accepted += 1
            else:
                duplicate += 1
        # A child can arrive before its parent. Re-evaluate those validation results after each batch.
        conn.execute(
            "UPDATE external_agent_spans AS child SET validation_result='valid' "
            "WHERE child.validation_result='missing_parent' AND child.parent_span_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM external_agent_spans AS parent WHERE parent.source_id=child.source_id "
            "AND parent.trace_id=child.trace_id AND parent.span_id=child.parent_span_id)"
        )
    return accepted, duplicate


def _rows(conn: sqlite3.Connection, source_id: str, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM external_agent_spans WHERE source_id=? AND run_id=? ORDER BY start_time, span_id",
        (source_id, run_id))
    result = []
    for row in rows:
        item = dict(row)
        item["attributes"] = json.loads(item["attributes"])
        result.append(item)
    return result


def list_runs(conn: sqlite3.Connection, source_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, MIN(environment) environment, COUNT(*) span_count, COUNT(DISTINCT unit_id) unit_count,"
        " MIN(start_time) start_time, MAX(end_time) end_time FROM external_agent_spans"
        " WHERE source_id=? GROUP BY run_id ORDER BY start_time DESC", (source_id,))
    return [dict(row) for row in rows]


def summary(conn: sqlite3.Connection, source_id: str, run_id: str) -> dict:
    spans = _rows(conn, source_id, run_id)
    units = {s["unit_id"] for s in spans if s["unit_id"]}
    workers = {s["worker_id"] for s in spans if s["worker_id"]}
    names = Counter(s["span_name"] for s in spans)
    completion = conn.execute(
        "SELECT * FROM external_agent_completions WHERE source_id=? AND run_id=?", (source_id, run_id)
    ).fetchone()
    return {
        "source_id": source_id, "run_id": run_id, "environment": spans[0]["environment"] if spans else None,
        "trace_status": "TRACE_COMPLETE" if completion and completion["status"] == "TRACE_COMPLETE" else "PARTIAL",
        "units": len(units), "workers": len(workers), "spans": len(spans), "span_counts": dict(names),
        "completed": sum(s["span_name"] == "aob.unit.completed" for s in spans),
        "failed": sum(s["span_name"] == "aob.unit.failed" for s in spans),
        "agent_classifications": sum(s["span_name"] == "aob.agent.classification" for s in spans),
        "tool_lookups": sum(s["span_name"] == "aob.tool.lookup" for s in spans),
        "validations": sum(s["span_name"] == "aob.result.validation" for s in spans),
        "application_spans": sum(s["span_name"].startswith("aob.") for s in spans),
        "llm_calls": 0, "token_usage": "N/A",
        "expected_spans": completion["expected_spans"] if completion else None,
        "expected_units": completion["expected_units"] if completion else None,
    }


def units(conn: sqlite3.Connection, source_id: str, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT unit_id, MIN(worker_id) worker_id, MIN(start_time) start_time, MAX(end_time) end_time,"
        " COUNT(*) span_count, SUM(span_name='aob.unit.failed') failed"
        " FROM external_agent_spans WHERE source_id=? AND run_id=? AND unit_id IS NOT NULL GROUP BY unit_id"
        " ORDER BY unit_id", (source_id, run_id))
    return [dict(row) for row in rows]


def workers(conn: sqlite3.Connection, source_id: str, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT worker_id, COUNT(*) span_count, COUNT(DISTINCT unit_id) unit_count,"
        " SUM(status='ERROR') error_count FROM external_agent_spans"
        " WHERE source_id=? AND run_id=? AND worker_id IS NOT NULL GROUP BY worker_id ORDER BY worker_id",
        (source_id, run_id))
    return [dict(row) for row in rows]


def timeline(conn: sqlite3.Connection, source_id: str, run_id: str) -> list[dict]:
    return _rows(conn, source_id, run_id)


def complete(conn: sqlite3.Connection, source_id: str, run_id: str, expected_spans: int, expected_units: int) -> dict:
    spans = _rows(conn, source_id, run_id)
    ids = {(s["trace_id"], s["span_id"]) for s in spans}
    complete = len(ids) == expected_spans and len({s["unit_id"] for s in spans if s["unit_id"]}) == expected_units
    complete = complete and all(not s["parent_span_id"] or
                                (s["trace_id"], s["parent_span_id"]) in ids for s in spans)
    status = "TRACE_COMPLETE" if complete else "PARTIAL"
    with db.tx(conn):
        conn.execute(
            "INSERT INTO external_agent_completions(source_id,run_id,expected_spans,expected_units,completed_at,status)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,run_id) DO UPDATE SET expected_spans=excluded.expected_spans,"
            "expected_units=excluded.expected_units,completed_at=excluded.completed_at,status=excluded.status",
            (source_id, run_id, expected_spans, expected_units, db.now(), status))
    return {"source_id": source_id, "run_id": run_id, "status": status, "received_spans": len(ids)}
