"""Durable admission and one bounded control iteration. History has no worker hook."""
from contextlib import closing
import json
import secrets
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

import config
import demo_auth
import demo_store
import kaggle_launcher
from storage import db
from . import resolution, store
from .azure import TRACE, uncertain
from .policy import Principal, ServiceError
from .providers import check_run_capability
from .registry import Label, scoped_deployment


class StartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_deployment_id: Label
    workload_version: Literal["synthetic-intake-v1", "kaggle-light-v1"]
    deadline_seconds: int = Field(default=120, ge=1, le=10800)


def enabled():
    if not (config.CROSS_CLOUD_ENABLED and config.REMOTE_RUNS_ENABLED):
        raise ServiceError(403, "EXECUTION_DISABLED")


def reserve(conn, principal, deployment, spec, key, traceparent):
    enabled()
    if not spec or spec.agent_deployment_id != deployment["deployment_id"]:
        raise ServiceError(422, "INVALID_REQUEST")
    source = deployment["source_id"]
    if not source or "status" not in deployment["capabilities"]:
        raise ServiceError(409, "NOT_SUPPORTED")
    principal.source(source)
    azure = deployment["provider"] == "azure"
    if (spec.workload_version != ("synthetic-intake-v1" if azure else "kaggle-light-v1")
            or spec.deadline_seconds > (120 if azure else config.DEMO_MAX_RUN_SECONDS)):
        raise ServiceError(422, "WORKLOAD_OR_DEADLINE")
    if traceparent is not None and not TRACE.fullmatch(traceparent):
        raise ServiceError(422, "INVALID_TRACE_CONTEXT")
    target = resolution.approved_target(deployment)

    def insert():
        current = scoped_deployment(conn, principal, deployment["deployment_id"])
        check_run_capability(principal, current)
        now = db.now()
        try:
            if azure:
                demo_store.check_dispatch_slot(conn)
                if demo_store.active_runs(conn):
                    raise ServiceError(409, "RUN_ACTIVE")
                remote = "azure-py-" + str(uuid4())
            else:
                remote = demo_store.reserve_run(conn, principal.owner, "light", 10, now)
        except demo_store.Conflict:
            raise ServiceError(409, "RUN_ACTIVE") from None
        except demo_store.RateLimited:
            raise ServiceError(429, "RATE_LIMITED") from None
        local = uuid4().hex
        conn.execute("INSERT INTO deployment_bindings(run_id,owner_id,source_id,remote_run_id,"
                     "agent_deployment_id,agent_version,workload_version,deadline,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (local, principal.owner, source, remote, current["deployment_id"], current["config_version"],
                      spec.workload_version, now + spec.deadline_seconds, now))
        conn.execute("INSERT INTO run_dispatch(run_id,updated_at) VALUES(?,?)", (local, now))
        parent = traceparent or f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
        conn.execute("INSERT INTO dispatch_context(run_id,snapshot,traceparent,next_check) VALUES(?,?,?,?)",
                     (local, json.dumps({"deployment": current, "target": target}), parent, now))
        return {"run_id": local}

    return store.record_once(conn, principal, "start", key, spec.model_dump(), insert)


def step():
    """Claim the only send right, or lease a GET. A lost send right never permits another POST."""
    enabled()
    with closing(store.writer()) as conn, db.tx(conn):
        now = db.now()
        row = conn.execute("""
            SELECT b.*,d.state,c.traceparent,c.snapshot FROM dispatch_context c JOIN run_dispatch d USING(run_id)
            JOIN deployment_bindings b USING(run_id) WHERE c.next_check<=? AND d.state!='terminal'
            ORDER BY c.next_check LIMIT 1""", (now,)).fetchone()
        if row is None:
            return False
        start = row["state"] == "reserved"
        claim, local = uuid4().hex, row["run_id"]
        conn.execute("UPDATE dispatch_context SET claim=?,next_check=? WHERE run_id=?", (claim, now+90, local))
        conn.execute("UPDATE run_dispatch SET state=?,attempts=attempts+?,updated_at=? WHERE run_id=?",
                     ("dispatching" if start else "uncertain", int(start), now, local))

    snapshot = json.loads(row["snapshot"])
    # This boundary intentionally catches only classified configuration/launcher errors.
    # A worker crash leaves a durable dispatching record; recovery may only GET.
    try:
        owner = row["owner_id"]
        p = Principal.from_verified({"sub": owner, "scope": "aob:runs:execute"},
                                    demo_auth.load_allowed_users().get(owner), config.get_otlp_read_sources())
        p.source(row["source_id"])
        with closing(store.reader()) as conn:
            current = scoped_deployment(conn, p, row["agent_deployment_id"])
        check_run_capability(p, current)
        if current != snapshot["deployment"]:
            raise ServiceError(403, "TARGET_CHANGED")
        if row["deadline"] <= db.now() or row["cancel_requested"]:
            raise ServiceError(409, "DEADLINE_EXPIRED" if not row["cancel_requested"] else "CANCEL_REQUESTED")
        client = resolution.resolve(current, snapshot["target"], row["deadline"])
        result = (client.start_run({"remote_run_id": row["remote_run_id"], "traceparent": row["traceparent"],
                                    "deadline": row["deadline"]})
                  if start else client.get_status(row["remote_run_id"]))
    except ServiceError as exc:
        result = ({"state": "terminal", "business_status": "failed", "code": exc.code} if start
                  else uncertain(exc.code))
    except kaggle_launcher.LaunchError:
        result = uncertain("LAUNCHER_ERROR")

    with closing(store.writer()) as conn, db.tx(conn):
        now = db.now()
        next_check = min(now+5, row["deadline"]) if result["state"] != "terminal" else None
        if now >= row["deadline"] and next_check is not None:
            result, next_check = uncertain("DEADLINE_EXPIRED"), None
        if conn.execute("UPDATE dispatch_context SET next_check=?,claim=NULL WHERE run_id=? AND claim=?",
                        (next_check, local, claim)).rowcount:
            conn.execute("UPDATE run_dispatch SET state=?,last_error_code=?,updated_at=? WHERE run_id=?",
                         (result["state"], result.get("code"), now, local))
            conn.execute("UPDATE deployment_bindings SET business_status=? WHERE run_id=?",
                         (result["business_status"], local))
            if start and result["state"] == "terminal" and snapshot["deployment"]["provider"] == "kaggle":
                demo_store.finish(conn, row["remote_run_id"], "failed", "Dispatch denied")
    return True
