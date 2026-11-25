"""Private registry transport. The sub-application keeps validation errors off legacy routes."""
from contextlib import closing

from fastapi import Depends, FastAPI, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import config
import demo_auth
from crosscloud.policy import Principal, ServiceError
from crosscloud.registry import DeploymentRegistryService, DeploymentSpec
from crosscloud.store import reader, writer


def principal(claims=Depends(demo_auth.require_user)):
    return Principal.from_verified(claims, demo_auth.load_allowed_users().get(claims["sub"]),
                                   config.get_otlp_read_sources())


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
registry = DeploymentRegistryService()


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
def deployments(limit: int = 50, offset: int = 0, user=Depends(principal)):
    with closing(reader()) as conn:
        return {"items": registry.list(conn, user, limit, offset)}


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
