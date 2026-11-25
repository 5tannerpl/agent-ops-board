"""Stored evidence for explicitly bound runs; no provider dependencies on the read path."""
import json
from contextlib import closing

import otlp_semantics
from .commands import scoped_binding
from .policy import ServiceError, page
from .store import digest, reader


class RunQueryService:
    def list_runs(self, principal, limit=50, offset=0):
        principal.require("aob:runs:read")
        where, args = principal.predicate()
        with closing(reader()) as conn:
            return [dict(row) for row in conn.execute(
                f"SELECT run_id,source_id,business_status,telemetry_status,created_at FROM deployment_bindings "
                f"WHERE {where} ORDER BY created_at DESC,run_id LIMIT ? OFFSET ?", [*args, *page(limit, offset)])]

    def get_run_summary(self, principal, run_id):
        principal.require("aob:runs:read")
        with closing(reader()) as conn:
            return scoped_binding(conn, principal, run_id)

    def get_run_trace(self, principal, run_id, limit=50, offset=0):
        principal.require("aob:traces:read")
        with closing(reader()) as conn:
            run = scoped_binding(conn, principal, run_id)
            rows = conn.execute("SELECT trace_id,span_id,parent_span_id,duration_ms FROM external_agent_spans "
                                "WHERE source_id=? AND run_id=? ORDER BY start_time,trace_id,span_id LIMIT ? OFFSET ?",
                                (run["source_id"], run["remote_run_id"], *page(limit, offset)))
            return [dict(row) for row in rows]

    @staticmethod
    def _evidence(principal, run_id):
        # Fail explicitly rather than aggregate a truncated run as if it were complete.
        with closing(reader()) as conn:
            run = scoped_binding(conn, principal, run_id)
            rows = conn.execute("SELECT unit_id,span_name,attributes FROM external_agent_spans "
                                "WHERE source_id=? AND run_id=? LIMIT 2001", (run["source_id"], run["remote_run_id"]))
            spans = []
            for row in rows:
                if len(spans) == 2000:
                    raise ServiceError(413, "EVIDENCE_LIMIT")
                span = dict(row)
                span["attributes"] = json.loads(span["attributes"])
                span["role"] = otlp_semantics.role(span)
                spans.append(span)
            return spans

    def get_failed_tasks(self, principal, run_id, limit=50, offset=0):
        principal.require("aob:runs:read")
        page(limit, offset)
        outcomes = otlp_semantics.outcomes(self._evidence(principal, run_id))
        # Producer-supplied unit labels can contain business text.
        return sorted(digest(unit) for unit, status in outcomes.items() if status == "FAILED")[offset:offset+limit]

    def get_llm_metrics(self, principal, run_id):
        principal.require("aob:metrics:read")
        spans = self._evidence(principal, run_id)
        metrics = otlp_semantics.metrics(spans)
        return {"observed_calls": metrics["llm_calls"] if spans else None,
                "usage": metrics["token_usage"] if metrics["llm_calls"] else None,
                "provenance": "measured" if spans else "unavailable"}

    def get_gpu_metrics(self, principal, run_id):
        principal.require("aob:metrics:read")
        with closing(reader()) as conn:
            scoped_binding(conn, principal, run_id)
        return {"active_replicas": None, "provenance": "unavailable"}
