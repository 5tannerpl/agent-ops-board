"""Registration validates metadata, not reachability, ownership proof or credentials."""
import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from storage import db
from .policy import ServiceError, page
from .store import record_once


Label = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")]
# References name operator configuration, never a URL, a secret value or an arbitrary blob.
Reference = Annotated[str, Field(pattern=r"^ref:[a-z][a-z0-9_.-]{0,63}$")]
Capability = Literal["inspect", "provision", "delete", "start", "status", "cancel", "inference", "metrics", "health"]


class DeploymentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Label
    runtime_type: Literal["agent", "inference"]
    ownership: Literal["external", "managed"] = "external"
    connection_mode: Literal["public_https", "private_connector", "push_only"] = "push_only"
    execution_policy: Literal["observe_only", "execute"] = "observe_only"
    endpoint_ref: Reference | None = None
    credential_ref: Reference | None = None
    source_id: Label | None = None
    image_ref: Reference | None = None
    image_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    provider_resource_id: Reference | None = None
    connector_ref: Reference | None = None
    telemetry_policy_ref: Reference | None = None
    capabilities: frozenset[Capability] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def endpoint_required(self):
        if self.connection_mode != "push_only" and not self.endpoint_ref:
            raise ValueError("endpoint reference required")
        if self.connection_mode == "push_only" and self.execution_policy == "execute":
            raise ValueError("push-only deployment cannot execute")
        return self


def scoped_deployment(conn, principal, deployment_id):
    where, args = principal.predicate()
    row = conn.execute(f"SELECT * FROM deployments WHERE deployment_id=? AND {where}",
                       [deployment_id, *args]).fetchone()
    if row is None:
        raise ServiceError(404, "NOT_FOUND")
    return deployment_view(row)


def deployment_view(row):
    return {**dict(row), "capabilities": json.loads(row["capabilities"])}


class DeploymentRegistryService:
    def register(self, conn, principal, spec: DeploymentSpec, key):
        principal.require("aob:deployments:write")
        principal.source(spec.source_id)
        values = spec.model_dump(mode="json")
        values["capabilities"] = sorted(spec.capabilities)

        def insert():
            now = db.now()
            fields = dict(values, capabilities=json.dumps(values["capabilities"]), owner_id=principal.owner,
                          deployment_id=uuid4().hex, created_at=now, updated_at=now)
            conn.execute(f"INSERT INTO deployments ({','.join(fields)}) VALUES "
                         f"({','.join('?' for _ in fields)})", list(fields.values()))
            return scoped_deployment(conn, principal, fields["deployment_id"])

        return record_once(conn, principal, "register", key, values, insert)

    def get(self, conn, principal, deployment_id):
        principal.require("aob:deployments:read")
        return scoped_deployment(conn, principal, deployment_id)

    def list(self, conn, principal, limit=50, offset=0):
        principal.require("aob:deployments:read")
        where, args = principal.predicate()
        rows = conn.execute(f"SELECT * FROM deployments WHERE {where} ORDER BY created_at DESC, "
                            "deployment_id LIMIT ? OFFSET ?", [*args, *page(limit, offset)])
        return [deployment_view(row) for row in rows]

    def unregister(self, conn, principal, deployment_id):
        principal.require("aob:deployments:write")
        with db.tx(conn):
            scoped_deployment(conn, principal, deployment_id)
            conn.execute("UPDATE deployments SET status='unregistered',updated_at=? WHERE deployment_id=? "
                         "AND status='registered'", (db.now(), deployment_id))
