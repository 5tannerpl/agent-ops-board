"""Private registry transport. The sub-application keeps validation errors off legacy routes."""
from contextlib import closing
from typing import Literal

from fastapi import Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import config
import demo_auth
from crosscloud.policy import Principal, ServiceError
from crosscloud.registry import DeploymentRegistryService, DeploymentSpec
from crosscloud.store import reader, writer
from crosscloud.commands import RunCommandService
from crosscloud.dispatch import StartSpec
from crosscloud.queries import RunQueryService


def principal(claims=Depends(demo_auth.require_user)):
    return Principal.from_verified(claims, demo_auth.load_allowed_users().get(claims["sub"]),
                                   config.get_otlp_read_sources())


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
registry = DeploymentRegistryService()


@app.post("/runs", status_code=202)
def create_run(spec: StartSpec, key: str = Header(alias="Idempotency-Key"),
               traceparent: str | None = Header(default=None), user=Depends(principal)):
    user.require("aob:runs:execute")
    with closing(writer()) as conn:
        return RunCommandService().start(conn, user, spec.agent_deployment_id, spec=spec,
                                         key=key, traceparent=traceparent)


@app.get("/runs/{run_id}")
def get_run(run_id: str, user=Depends(principal)):
    return RunQueryService().get_run_summary(user, run_id)


@app.get("/runs")
def binding_history(limit: int = 50, offset: int = 0, search: str = "", provider: str = "",
                    business: str = "", since: float | None = None, until: float | None = None,
                    deployment: str = "", user=Depends(principal)):
    return RunQueryService().bindings(user, limit=limit, offset=offset, search=search, provider=provider,
                                      business=business, since=since, until=until, deployment=deployment)


@app.get("/observations/count")
def span_count(user=Depends(principal)):
    return {"span_count": RunQueryService().received_spans(user)}


@app.get("/runs/{run_id}/observations/{section}")
def observation(run_id: str, section: Literal["summary", "units", "workers", "timeline"],
                limit: int = 100, offset: int = 0, unit: str | None = None, user=Depends(principal)):
    return RunQueryService().observation(user, run_id, section, limit, offset, unit)


@app.get("/runs/{run_id}/llm-metrics")
def llm_metrics(run_id: str, user=Depends(principal)):
    user.require("aob:runs:read")
    return RunQueryService().get_llm_metrics(user, run_id)


@app.exception_handler(ServiceError)
async def service_error(request, exc):
    return JSONResponse({"code": exc.code}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def invalid_input(request, exc):
    return JSONResponse({"code": "INVALID_REQUEST"}, status_code=422)


@app.post("/deployments", status_code=201)
def register(spec: DeploymentSpec, key: str = Header(alias="Idempotency-Key"), user=Depends(principal)):
    with closing(writer()) as conn:
        return registry.register(conn, user, spec, key)


@app.get("/deployments")
def deployments(limit: int = 50, offset: int = 0, workspace: bool = False, user=Depends(principal)):
    with closing(reader()) as conn:
        rows = registry.list(conn, user, limit, offset)
        if workspace:
            fields = ("deployment_id", "provider", "runtime_type", "ownership", "connection_mode",
                      "execution_policy", "capabilities", "source_id", "status")
            rows = [{key: row[key] for key in fields} for row in rows]
        return {"items": rows}


@app.get("/deployments/{deployment_id}")
def get_deployment(deployment_id: str, user=Depends(principal)):
    with closing(reader()) as conn:
        return registry.get(conn, user, deployment_id)


@app.post("/deployments/{deployment_id}/validate")
def validate(deployment_id: str, user=Depends(principal)):
    with closing(reader()) as conn:
        record = registry.get(conn, user, deployment_id)
    if record["status"] != "registered":
        raise ServiceError(409, "UNREGISTERED")
    DeploymentSpec.model_validate({key: record[key] for key in DeploymentSpec.model_fields})
    return {"deployment_id": deployment_id, "configuration_valid": True, "reachability": "unknown"}
