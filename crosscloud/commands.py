"""Record observations and local intent. None of these commands dispatch work."""
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, FiniteFloat

from storage import db
from .policy import ServiceError
from .providers import check_run_capability
from .registry import Label, scoped_deployment
from .store import record_once


class BindingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_deployment_id: Label
    inference_deployment_id: Label | None = None
    source_id: Label
    remote_run_id: Label
    workload_version: Label
    deadline: FiniteFloat


def scoped_binding(conn, principal, run_id):
    where, args = principal.predicate()
    row = conn.execute(f"SELECT * FROM deployment_bindings WHERE run_id=? AND {where}",
                       [run_id, *args]).fetchone()
    if row is None:
        raise ServiceError(404, "NOT_FOUND")
    return dict(row)


class RunCommandService:
    def start(self, conn, principal, deployment_id):
        check_run_capability(principal, scoped_deployment(conn, principal, deployment_id))
        raise ServiceError(409, "NOT_SUPPORTED")  # no dispatch adapters in 4.2A

    def bind(self, conn, principal, spec: BindingSpec, key):
        principal.require("aob:runs:write")
        principal.source(spec.source_id)

        def reserve():
            versions = {}
            for role, deployment_id in (("agent", spec.agent_deployment_id),
                                        ("inference", spec.inference_deployment_id)):
                if deployment_id is None:
                    continue
                deployment = scoped_deployment(conn, principal, deployment_id)
                if (deployment["runtime_type"] != role or deployment["status"] != "registered"
                        or deployment["source_id"] not in (None, spec.source_id)):
                    raise ServiceError(409, "INCOMPATIBLE_DEPLOYMENT")
                versions[role] = deployment["config_version"]
            run_id = uuid4().hex
            cursor = conn.execute(
                "INSERT INTO deployment_bindings(run_id,owner_id,source_id,remote_run_id,agent_deployment_id,"
                "agent_version,inference_deployment_id,inference_version,workload_version,deadline,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,remote_run_id) DO NOTHING",
                (run_id, principal.owner, spec.source_id, spec.remote_run_id, spec.agent_deployment_id,
                 versions["agent"], spec.inference_deployment_id, versions.get("inference"),
                 spec.workload_version, spec.deadline, db.now()))
            if not cursor.rowcount:
                raise ServiceError(409, "ALREADY_BOUND")
            return {"run_id": run_id}

        return record_once(conn, principal, "bind", key, spec.model_dump(), reserve)

    def request_cancel(self, conn, principal, run_id):
        principal.require("aob:runs:write")
        with db.tx(conn):
            scoped_binding(conn, principal, run_id)
            conn.execute("UPDATE deployment_bindings SET cancel_requested=1 WHERE run_id=?", (run_id,))
        return {"cancel_requested": True, "remote_cancel": "NOT_SUPPORTED"}

    def record_status(self, conn, principal, run_id, *, business=None, telemetry=None):
        principal.require("aob:runs:write")
        if (business not in (None, "unknown", "running", "complete", "failed", "cancelled")
                or telemetry not in (None, "unknown", "partial", "complete")):
            raise ServiceError(422, "INVALID_STATUS")
        with db.tx(conn):
            scoped_binding(conn, principal, run_id)
            conn.execute("UPDATE deployment_bindings SET business_status=coalesce(?,business_status), "
                         "telemetry_status=coalesce(?,telemetry_status) WHERE run_id=?", (business, telemetry, run_id))
