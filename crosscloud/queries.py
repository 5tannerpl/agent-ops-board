"""Stored evidence for explicitly bound runs; no provider dependencies on the read path."""
from contextlib import closing

import otlp_semantics
from .history import owned_history
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
                f"WHERE {where} UNION ALL SELECT run_id,NULL,status,'unknown',created_at FROM demo_runs "
                "WHERE owner_sub=? ORDER BY created_at DESC,run_id LIMIT ? OFFSET ?",
                [*args, principal.owner, *page(limit, offset)])]

    def get_run_summary(self, principal, run_id):
        principal.require("aob:runs:read")
        with closing(reader()) as conn:
            if run_id.startswith("run-"):
                legacy = owned_history(conn, principal.owner, run_id)
                if legacy is None:
                    raise ServiceError(404, "NOT_FOUND")
                return {"run_id": legacy["run_id"], "business_status": legacy["status"],
                        "telemetry_status": "unknown", "created_at": legacy["created_at"]}
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
            # Only project the fields used by the shared outcome/usage rules, never raw bodies.
            rows = conn.execute("""
                SELECT unit_id,span_name,
                    json_extract(attributes,'$."aob.attempt"') AS attempt,
                    json_extract(attributes,'$."aob.task.status"') AS task_status,
                    json_extract(attributes,'$."gen_ai.operation.name"') AS operation,
                    json_extract(attributes,'$."gen_ai.usage.input_tokens"') AS input_tokens,
                    json_extract(attributes,'$."gen_ai.usage.output_tokens"') AS output_tokens
                FROM external_agent_spans WHERE source_id=? AND run_id=? LIMIT 2001
                """, (run["source_id"], run["remote_run_id"]))
            spans = []
            for row in rows:
                if len(spans) == 2000:
                    raise ServiceError(413, "EVIDENCE_LIMIT")
                attrs = {"aob.attempt": row["attempt"], "gen_ai.operation.name": row["operation"],
                         "gen_ai.usage.input_tokens": row["input_tokens"],
                         "gen_ai.usage.output_tokens": row["output_tokens"]}
                if row["task_status"] is not None:
                    attrs["aob.task.status"] = row["task_status"]
                span = {"unit_id": row["unit_id"], "span_name": row["span_name"], "attributes": attrs}
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
