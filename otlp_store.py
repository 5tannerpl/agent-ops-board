"""Persistence and query helpers for traces received from external agents."""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter

import otlp_semantics as semantics
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
CREATE TABLE IF NOT EXISTS external_agent_conflicts (
  source_id TEXT NOT NULL, run_id TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
  PRIMARY KEY(source_id, run_id, trace_id, span_id)
);
"""

EXTRA = {"resource": "TEXT", "start_time_unix_nano": "TEXT", "end_time_unix_nano": "TEXT",
         "kind": "INTEGER", "scope_name": "TEXT", "scope_version": "TEXT"}


class IdentityConflict(ValueError):
    pass


class ManifestConflict(ValueError):
    pass


def connect(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    with db.tx(conn):
        for table, columns in [("external_agent_spans", EXTRA), ("external_agent_completions", {"external_parents": "TEXT"})]:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def insert_spans(conn: sqlite3.Connection, spans: list[dict]) -> tuple[int, int]:
    accepted = duplicate = 0
    conflict = False
    changed_runs = set()
    with db.tx(conn):
        conn.execute("SAVEPOINT batch")
        for span in spans:
            row = span.copy()
            for key in ("attributes", "resource"):
                row[key] = json.dumps(row[key], separators=(",", ":"), sort_keys=True)
            columns = ("source_id", "environment", "run_id", "unit_id", "worker_id", "trace_id", "span_id",
                       "parent_span_id", "span_name", "start_time", "end_time", "duration_ms", "status",
                       "validation_result", "attributes", *EXTRA)
            cur = conn.execute(f"INSERT INTO external_agent_spans({','.join(columns)}) "
                               f"VALUES({','.join(':'+k for k in columns)}) "
                               "ON CONFLICT(source_id,trace_id,span_id) DO NOTHING", row)
            if cur.rowcount == 1:
                accepted += 1
                changed_runs.add((span["source_id"], span["run_id"]))
                continue
            prev = conn.execute("SELECT * FROM external_agent_spans WHERE source_id=:source_id "
                                "AND trace_id=:trace_id AND span_id=:span_id", row).fetchone()
            if all(prev[k] == row[k] for k in columns):
                duplicate += 1
                continue
            conn.execute("ROLLBACK TO batch")
            changed_runs.clear()
            for run_id in {prev["run_id"], row["run_id"]}:
                changed_runs.add((row["source_id"], run_id))
                conn.execute("INSERT OR IGNORE INTO external_agent_conflicts VALUES(?,?,?,?)",
                             (row["source_id"], run_id, row["trace_id"], row["span_id"]))
            conflict = True
            break
        conn.execute("RELEASE batch")
        for source, run in changed_runs:
            if conn.execute("SELECT 1 FROM external_agent_completions WHERE source_id=? AND run_id=?",
                            (source, run)).fetchone():
                _refresh_completion(conn, source, run)
    if conflict:
        raise IdentityConflict()
    return accepted, duplicate


def _rows(conn: sqlite3.Connection, source_id: str, run_id: str, *, trace_id=None, worker_id=None,
          unit_id=None, limit=-1, offset=0) -> list[dict]:
    filters = {"source_id": source_id, "run_id": run_id}
    filters.update({k: v for k, v in (("trace_id", trace_id), ("worker_id", worker_id), ("unit_id", unit_id))
                    if v is not None})
    where = " AND ".join(f"{k}=:{k}" for k in filters)
    rows = conn.execute(f"SELECT * FROM external_agent_spans WHERE {where} ORDER BY start_time, "
                        "length(start_time_unix_nano), start_time_unix_nano, trace_id, span_id LIMIT :limit OFFSET :offset",
                        dict(filters, limit=limit, offset=offset))
    result = []
    for row in rows:
        item = dict(row)
        item["attributes"] = json.loads(item["attributes"])
        item["role"] = semantics.role(item)
        item["resource"] = json.loads(item["resource"]) if item["resource"] is not None else None
        if not item["environment"] or (item["resource"] is None and item["environment"] == "local"):
            item["environment"] = None
        result.append(item)
    return result


def list_runs(conn: sqlite3.Connection, source_id: str, limit=-1, offset=0) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, MIN(CASE WHEN resource IS NULL AND environment='local' THEN NULL "
        "ELSE NULLIF(environment,'') END) environment, COUNT(*) span_count, COUNT(DISTINCT unit_id) unit_count,"
        " MIN(start_time) start_time, MAX(end_time) end_time FROM external_agent_spans"
        " WHERE source_id=? GROUP BY run_id ORDER BY start_time DESC, run_id LIMIT ? OFFSET ?", (source_id, limit, offset))
    return [dict(row) for row in rows]


def summary(conn: sqlite3.Connection, source_id: str, run_id: str) -> dict:
    spans = _rows(conn, source_id, run_id)
    units = {s["unit_id"] for s in spans if s["unit_id"]}
    workers = {s["worker_id"] for s in spans if s["worker_id"]}
    names = Counter(s["span_name"] for s in spans)
    completion = conn.execute(
        "SELECT * FROM external_agent_completions WHERE source_id=? AND run_id=?", (source_id, run_id)
    ).fetchone()
    boundaries = set(map(tuple, json.loads(completion["external_parents"] or "[]"))) if completion else set()
    graph = semantics.graph(spans, boundaries)
    conflicts = conn.execute("SELECT COUNT(*) FROM external_agent_conflicts WHERE source_id=? AND run_id=?",
                             (source_id, run_id)).fetchone()[0]
    complete = (completion and completion["expected_spans"] == len(spans) and completion["expected_units"] == len(units)
                and not any((conflicts, graph["unresolved_parent_links"], graph["cyclic_parent_links"],
                             graph["unmatched_boundaries"])))
    environments = {s["environment"] for s in spans}
    return {
        "schema_version": 1, "source_id": source_id, "run_id": run_id,
        "environment": environments.pop() if len(environments) == 1 else None,
        "trace_status": "TRACE_COMPLETE" if complete else "PARTIAL",
        "trace_count": len({s["trace_id"] for s in spans}), "span_count": len(spans),
        "unique_units": len(units), "worker_count": len(workers), "identity_conflicts": conflicts,
        "units": len(units), "workers": len(workers), "spans": len(spans), "span_counts": dict(names),
        **graph, **semantics.metrics(spans),
        "expectation_origin": "source_manifest" if completion else None,
        "expected_spans": completion["expected_spans"] if completion else None,
        "expected_units": completion["expected_units"] if completion else None,
    }


def units(conn: sqlite3.Connection, source_id: str, run_id: str) -> list[dict]:
    outcomes = semantics.outcomes(_rows(conn, source_id, run_id))
    rows = conn.execute(
        "SELECT unit_id, CASE WHEN COUNT(DISTINCT worker_id)=1 THEN MIN(worker_id) END worker_id,"
        " MIN(start_time) start_time, MAX(end_time) end_time,"
        " COUNT(*) span_count"
        " FROM external_agent_spans WHERE source_id=? AND run_id=? AND unit_id IS NOT NULL GROUP BY unit_id"
        " ORDER BY unit_id", (source_id, run_id))
    return [dict(row, outcome=outcomes.get(row["unit_id"], "UNKNOWN"),
                 failed=int(outcomes.get(row["unit_id"]) == "FAILED")) for row in rows]


def workers(conn: sqlite3.Connection, source_id: str, run_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT worker_id, COUNT(*) span_count, COUNT(DISTINCT unit_id) unit_count,"
        " SUM(status='ERROR') error_count FROM external_agent_spans"
        " WHERE source_id=? AND run_id=? AND worker_id IS NOT NULL GROUP BY worker_id ORDER BY worker_id",
        (source_id, run_id))
    return [dict(row) for row in rows]


def timeline(conn: sqlite3.Connection, source_id: str, run_id: str, **filters) -> list[dict]:
    return _rows(conn, source_id, run_id, **filters)


def traces(conn, source_id, run_id, *, trace_id=None, limit=100, offset=0):
    where = "source_id=? AND run_id=?"
    params = [source_id, run_id]
    if trace_id is not None:
        where += " AND trace_id=?"
        params.append(trace_id)
    return [dict(row) for row in conn.execute(
        "SELECT trace_id, COUNT(*) span_count, COUNT(DISTINCT unit_id) unique_units, "
        "COUNT(DISTINCT worker_id) worker_count, MIN(start_time) start_time, MAX(end_time) end_time "
        f"FROM external_agent_spans WHERE {where} GROUP BY trace_id ORDER BY start_time, trace_id LIMIT ? OFFSET ?",
        (*params, limit, offset))]


def parse_boundaries(items):
    if not isinstance(items, list) or len(items) > 1000:
        raise ValueError
    edges = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"trace_id", "span_id", "parent_span_id"}:
            raise ValueError
        edge = tuple(item[k] for k in ("trace_id", "span_id", "parent_span_id"))
        if not all(isinstance(v, str) and re.fullmatch(f"[0-9a-f]{{{width}}}", v) and int(v, 16)
                   for v, width in zip(edge, (32, 16, 16))):
            raise ValueError
        edges.add(edge)
    return sorted(edges)


def _refresh_completion(conn, source_id, run_id):
    result = summary(conn, source_id, run_id)
    conn.execute("UPDATE external_agent_completions SET status=? WHERE source_id=? AND run_id=?",
                 (result["trace_status"], source_id, run_id))
    return result


def complete(conn: sqlite3.Connection, source_id: str, run_id: str, expected_spans: int, expected_units: int,
             boundaries=()) -> dict:
    external = json.dumps(boundaries)
    with db.tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO external_agent_completions(source_id,run_id,expected_spans,expected_units,"
            "completed_at,status,external_parents) VALUES(?,?,?,?,?,'PARTIAL',?)",
            (source_id, run_id, expected_spans, expected_units, db.now(), external))
        manifest = conn.execute("SELECT expected_spans,expected_units,COALESCE(external_parents,'[]') "
                                "FROM external_agent_completions WHERE source_id=? AND run_id=?",
                                (source_id, run_id)).fetchone()
        if tuple(manifest) != (expected_spans, expected_units, external):
            raise ManifestConflict()
        result = _refresh_completion(conn, source_id, run_id)
    return {"source_id": source_id, "run_id": run_id, "status": result["trace_status"],
            "received_spans": result["span_count"]}
