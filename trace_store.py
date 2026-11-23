"""Bounded OpenTelemetry trace batch persistence and correlation helpers."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

import otlp_semantics as semantics
from storage import db

TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")
SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{8,32}$")
MAX_SPANS_PER_BATCH = 250
MAX_ATTRIBUTE_LENGTH = 256
ALLOWED_ATTRIBUTES = {
    "aob.run_id", "aob.unit_id", "aob.worker_id", "aob.attempt", "aob.task_id",
    "aob.scaling_mode", "aob.units", "gen_ai.operation.name", "gen_ai.request.model",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "service.name",
    "service.version", "deployment.environment.name",
}
ALLOWED_RESOURCE_ATTRIBUTES = {"service.name", "service.version", "deployment.environment.name", "aob.source"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_batches (
  run_id TEXT NOT NULL, batch_id TEXT NOT NULL, submitted_at REAL NOT NULL,
  accepted_span_count INTEGER NOT NULL, rejected_count INTEGER NOT NULL DEFAULT 0,
  duplicate_count INTEGER NOT NULL DEFAULT 0, batch_status TEXT NOT NULL,
  schema_version INTEGER NOT NULL, finalized INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, batch_id)
);
CREATE TABLE IF NOT EXISTS trace_spans (
  run_id TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL,
  parent_span_id TEXT, name TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
  start_ts REAL NOT NULL, end_ts REAL NOT NULL, status TEXT NOT NULL,
  attributes TEXT NOT NULL, resource_metadata TEXT NOT NULL,
  PRIMARY KEY (run_id, trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS ix_trace_spans_run_time ON trace_spans(run_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_trace_spans_run_unit ON trace_spans(run_id, json_extract(attributes, '$.aob.unit_id'));
"""


class TraceValidationError(ValueError):
    pass


def _limited_map(value, allowed: set[str]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TraceValidationError("attributes must be an object")
    result = {}
    for key, item in value.items():
        if key not in allowed:
            raise TraceValidationError("attribute is not allowed")
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (str, int, float)) and len(str(item)) <= MAX_ATTRIBUTE_LENGTH:
            result[key] = item
        else:
            raise TraceValidationError("attribute value is invalid or too long")
    return result


def _timestamp(value) -> tuple[str, float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1_000_000_000
        text = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    elif isinstance(value, str):
        text = value
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise TraceValidationError("timestamp is invalid") from exc
    else:
        raise TraceValidationError("timestamp is invalid")
    return text[:64], timestamp


def validate_span(raw: dict, run_id: str) -> dict:
    if not isinstance(raw, dict):
        raise TraceValidationError("span must be an object")
    trace_id, span_id = raw.get("trace_id"), raw.get("span_id")
    if not isinstance(trace_id, str) or not TRACE_ID_RE.fullmatch(trace_id):
        raise TraceValidationError("trace_id is invalid")
    if not isinstance(span_id, str) or not SPAN_ID_RE.fullmatch(span_id):
        raise TraceValidationError("span_id is invalid")
    parent = raw.get("parent_span_id") or raw.get("parent_id")
    if parent is not None and (not isinstance(parent, str) or not SPAN_ID_RE.fullmatch(parent)):
        raise TraceValidationError("parent_span_id is invalid")
    name = raw.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 128:
        raise TraceValidationError("span name is invalid")
    start_text, start_ts = _timestamp(raw.get("start_time", raw.get("start_time_unix_nano")))
    end_text, end_ts = _timestamp(raw.get("end_time", raw.get("end_time_unix_nano")))
    if end_ts < start_ts:
        raise TraceValidationError("span end precedes start")
    attrs = _limited_map(raw.get("attributes"), ALLOWED_ATTRIBUTES)
    resource = raw.get("resource")
    if isinstance(resource, dict) and "attributes" in resource:
        resource = resource["attributes"]
    resource = _limited_map(resource, ALLOWED_RESOURCE_ATTRIBUTES)
    status = raw.get("status")
    if isinstance(status, dict):
        status = status.get("status_code") or status.get("code") or "UNSET"
    if not isinstance(status, str) or status not in {"UNSET", "OK", "ERROR"}:
        status = "UNSET"
    if attrs.get("aob.run_id") not in (None, run_id):
        raise TraceValidationError("span run binding mismatch")
    return {
        "trace_id": trace_id.lower(), "span_id": span_id.lower(), "parent_span_id": parent.lower() if parent else None,
        "name": name, "start_utc": start_text, "end_utc": end_text, "start_ts": start_ts, "end_ts": end_ts,
        "status": status, "attributes": attrs, "resource_metadata": resource,
    }


def connect(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def ingest(conn: sqlite3.Connection, run_id: str, batch_id: str, spans: list[dict],
           schema_version: int, finalized: bool) -> dict:
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", batch_id):
        raise TraceValidationError("batch_id is invalid")
    if not isinstance(spans, list) or not spans or len(spans) > MAX_SPANS_PER_BATCH:
        raise TraceValidationError("spans must contain 1 to 250 items")
    checked = [validate_span(span, run_id) for span in spans]
    with db.tx(conn):
        existing = conn.execute("SELECT accepted_span_count, rejected_count, duplicate_count, finalized "
                                "FROM trace_batches WHERE run_id=? AND batch_id=?", (run_id, batch_id)).fetchone()
        if existing:
            return {"status": "duplicate", "accepted": 0, "duplicates": existing["duplicate_count"],
                    "rejected": existing["rejected_count"], "finalized": bool(existing["finalized"])}
        accepted = duplicates = 0
        for span in checked:
            cur = conn.execute(
                "INSERT OR IGNORE INTO trace_spans(run_id, trace_id, span_id, parent_span_id, name, start_utc, "
                "end_utc, start_ts, end_ts, status, attributes, resource_metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, span["trace_id"], span["span_id"], span["parent_span_id"], span["name"],
                 span["start_utc"], span["end_utc"], span["start_ts"], span["end_ts"], span["status"],
                 json.dumps(span["attributes"], separators=(",", ":")),
                 json.dumps(span["resource_metadata"], separators=(",", ":"))))
            if cur.rowcount:
                accepted += 1
            else:
                duplicates += 1
        conn.execute(
            "INSERT INTO trace_batches(run_id,batch_id,submitted_at,accepted_span_count,rejected_count,"
            "duplicate_count,batch_status,schema_version,finalized) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, batch_id, db.now(), accepted, 0, duplicates, "complete", schema_version, int(finalized)))
    return {"status": "accepted", "accepted": accepted, "duplicates": duplicates, "rejected": 0,
            "finalized": finalized}


def _rows(conn, run_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM trace_spans WHERE run_id=? ORDER BY start_ts, span_id", (run_id,))
    result = []
    for row in rows:
        item = dict(row)
        item["attributes"] = json.loads(item["attributes"])
        item["resource_metadata"] = json.loads(item["resource_metadata"])
        item["duration_ms"] = max(0.0, (item["end_ts"] - item["start_ts"]) * 1000)
        attrs = item["attributes"]
        item["span_name"] = item["name"]
        item["role"] = semantics.role(item)
        item["unit_id"] = attrs.get("aob.unit_id")
        task_id = attrs.get("aob.task_id")
        item["task_id"] = str(task_id) if type(task_id) in (int, str) else None
        result.append(item)
    return result


def _correlate(spans: list[dict]) -> None:
    by_id = {(s["trace_id"], s["span_id"]): s for s in spans}
    for span in spans:
        attrs = span["attributes"]
        if span["name"].startswith("chat ") or attrs.get("gen_ai.operation.name") == "chat":
            current = span
            seen = set()
            while current and (current["trace_id"], current["span_id"]) not in seen:
                seen.add((current["trace_id"], current["span_id"]))
                current_attrs = current["attributes"]
                if current_attrs.get("aob.unit_id"):
                    for key in ("aob.unit_id", "aob.worker_id", "aob.attempt"):
                        attrs.setdefault(key, current_attrs.get(key))
                    break
                parent = current["parent_span_id"]
                current = by_id.get((current["trace_id"], parent)) if parent else None
            span["correlation_status"] = "correlated" if attrs.get("aob.unit_id") else "UNATTRIBUTED"


def summary(conn: sqlite3.Connection, run_id: str, expected_units: int | None = None) -> dict:
    spans = _rows(conn, run_id)
    _correlate(spans)
    calls = [s for s in spans if s["name"].startswith("chat ") or s["attributes"].get("gen_ai.operation.name") == "chat"]
    tasks = [s for s in spans if s["name"] == "aob.worker.task"]
    validations = [s for s in spans if s["name"] == "aob.result.validation"]
    durations = sorted(s["duration_ms"] for s in calls)
    def percentile(p):
        if not durations:
            return None
        index = min(len(durations) - 1, int((len(durations) - 1) * p))
        return durations[index]
    input_count = sum(isinstance(s["attributes"].get("gen_ai.usage.input_tokens"), int) for s in calls)
    output_count = sum(isinstance(s["attributes"].get("gen_ai.usage.output_tokens"), int) for s in calls)
    models = sorted({s["attributes"].get("gen_ai.request.model") for s in calls if s["attributes"].get("gen_ai.request.model")})
    units = {s["attributes"].get("aob.unit_id") for s in tasks if s["attributes"].get("aob.unit_id")}
    batches = conn.execute("SELECT COALESCE(SUM(accepted_span_count),0),COALESCE(SUM(rejected_count),0),"
                           "COALESCE(SUM(duplicate_count),0),MAX(finalized) FROM trace_batches WHERE run_id=?",
                           (run_id,)).fetchone()
    # The signed final flag is producer evidence, not an OTLP manifest or proof of graph completeness.
    environments = {s["resource_metadata"].get("deployment.environment.name") for s in spans}
    v1 = {"schema_version": 1, "source_id": "signed:kaggle", "run_id": run_id,
          "environment": environments.pop() if len(environments) == 1 else None,
          "trace_status": "PARTIAL", "reported_final": bool(batches[3]), "expected_spans": None,
          "expected_units": expected_units, "expectation_origin": "owned_run" if expected_units is not None else None,
          "span_count": len(spans), "trace_count": len({s["trace_id"] for s in spans}),
          "unique_units": len({s["unit_id"] for s in spans if s["unit_id"]}),
          "worker_count": len({s["attributes"]["aob.worker_id"] for s in spans if s["attributes"].get("aob.worker_id")}),
          **semantics.graph(spans, set()), **semantics.metrics(spans)}
    return {"schema_v1": v1, "trace_status": "complete" if batches[3] else ("partial" if spans else "pending"),
            "spans": len(spans), "llm_calls": len(calls), "task_spans": len(tasks),
            "validation_spans": len(validations), "unique_units": len(units), "expected_units": expected_units,
            "task_status_ok": sum(s["status"] == "OK" for s in tasks),
            "task_status_error": sum(s["status"] == "ERROR" for s in tasks),
            "validation_status_ok": sum(s["status"] == "OK" for s in validations),
            "validation_status_error": sum(s["status"] == "ERROR" for s in validations),
            "models": models, "input_tokens": sum(s["attributes"].get("gen_ai.usage.input_tokens", 0)
                                                  for s in calls if isinstance(s["attributes"].get("gen_ai.usage.input_tokens"), int)),
            "output_tokens": sum(s["attributes"].get("gen_ai.usage.output_tokens", 0)
                                 for s in calls if isinstance(s["attributes"].get("gen_ai.usage.output_tokens"), int)),
            "input_token_coverage": f"{input_count}/{len(calls)}", "output_token_coverage": f"{output_count}/{len(calls)}",
            "llm_avg_ms": sum(durations) / len(durations) if durations else None,
            "llm_p50_ms": percentile(.50), "llm_p95_ms": percentile(.95),
            "accepted_spans": batches[0], "rejected_spans": batches[1], "duplicate_spans": batches[2],
            "unattributed_calls": sum(s.get("correlation_status") == "UNATTRIBUTED" for s in calls),
            "finalized": bool(batches[3]),
    }


def calls(conn: sqlite3.Connection, run_id: str, limit: int, offset: int, sort: str,
          unit_id: str | None = None, worker_id: str | None = None, attempt: int | None = None) -> dict:
    spans = _rows(conn, run_id)
    _correlate(spans)
    items = [s for s in spans if s["name"].startswith("chat ") or s["attributes"].get("gen_ai.operation.name") == "chat"]
    if unit_id:
        items = [s for s in items if s["attributes"].get("aob.unit_id") == unit_id]
    if worker_id:
        items = [s for s in items if s["attributes"].get("aob.worker_id") == worker_id]
    if attempt is not None:
        items = [s for s in items if s["attributes"].get("aob.attempt") == attempt]
    items.sort(key=lambda s: (s["duration_ms"], s["span_id"]) if sort == "duration" else (s["start_ts"], s["span_id"]))
    return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}


def unit_trace(conn: sqlite3.Connection, run_id: str, unit_id: str) -> dict:
    spans = _rows(conn, run_id)
    _correlate(spans)
    selected = [s for s in spans if s["attributes"].get("aob.unit_id") == unit_id]
    return {"run_id": run_id, "unit_id": unit_id, "spans": selected, "workflow_available": False}
