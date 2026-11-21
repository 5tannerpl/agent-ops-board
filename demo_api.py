"""Live-demo private API and public event ingress."""
import json
import logging
import re
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

import config
import demo_auth
import demo_scaling
import demo_service
import demo_store as store
import trace_store
from api.response_models import error_code
from security.event_schemas import rejection_diagnostics, validate_payload

log = logging.getLogger("demo_api")
private_router = APIRouter()
ingress_router = APIRouter()
_EVENT_TYPE = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_EVENT_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_VALIDATION_EVENTS = (
    "notebook_started", "environment_ready", "model_located", "model_ready", "model_validation_started",
    "worker_validation_completed", "modelcheck_completed", "model_validation_completed",
)
_VALIDATION_STATUSES = {"passed", "failed"}


def get_conn():
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


def run_view(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "kaggle_status": run["kaggle_status"],
        "error_code": error_code(run.get("error")),
        "mode": run.get("mode"),
        "units": run.get("units"),
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "finished_at": run["finished_at"],
    }


def _validation_status(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    return status if isinstance(status, str) and status in _VALIDATION_STATUSES else None


def owned_run(conn, run_id: str, user: dict) -> dict:
    run = store.get_run(conn, run_id) if store.RUN_ID_RE.match(run_id) else None
    if run is None or run["owner_sub"] != user["sub"]:
        raise HTTPException(404, "run not found")
    return demo_service.sync_run(conn, run)


@private_router.get("/api/me")
def me(user: dict = Depends(demo_auth.require_demo_access)):
    return {"user_id": user["sub"], "can_start_demo": True}


class StartIn(BaseModel):
    mode: Literal["light", "high_concurrency"] = "light"
    units: Literal[50, 100, 150] | None = None


@private_router.post("/api/demo/start", status_code=202)
def start_demo(background: BackgroundTasks, body: StartIn | None = None,
               user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    body = body or StartIn()
    if body.mode == "light" and body.units is not None:
        raise HTTPException(422, "units can only be chosen for high_concurrency")
    units = body.units or (100 if body.mode == "high_concurrency" else 10)
    try:
        run = demo_service.start_run(conn, user["sub"], body.mode, units)
    except store.Conflict:
        raise HTTPException(409, "A demo is already queued or running")
    except store.RateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)})
    background.add_task(demo_service.submit_run, run["run_id"])
    return run_view(run)


@private_router.get("/api/demo/runs")
def list_runs(user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    runs = [demo_service.sync_run(conn, r) for r in store.list_runs(conn, user["sub"])]
    return {"items": [run_view(r) for r in runs]}


@private_router.get("/api/demo/{run_id}/status")
def run_status(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    run = owned_run(conn, run_id, user)
    return {**run_view(run), **demo_service.summarize_run(conn, run)}


@private_router.get("/api/demo/{run_id}/state")
def run_state(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    run = owned_run(conn, run_id, user)
    return {**run_view(run), **demo_scaling.state(conn, run)}


@private_router.get("/api/demo/{run_id}/metrics")
def run_metrics(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    return demo_scaling.metrics(conn, owned_run(conn, run_id, user))


@private_router.get("/api/demo/{run_id}/summary")
def run_summary(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    return demo_scaling.summary(conn, owned_run(conn, run_id, user))


@private_router.get("/api/demo/{run_id}/validation")
def run_validation(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    owned_run(conn, run_id, user)
    events = store.list_events(conn, run_id, 0, config.EVENT_MAX_PER_RUN, _VALIDATION_EVENTS)
    return {"items": [{"id": e["id"], "received_at": e["received_at"], "event_type": e["event_type"],
                       "status": _validation_status(e["payload"])} for e in events]}


class EventIn(BaseModel):
    event_id: str = Field(pattern=_EVENT_ID)
    run_id: str = Field(pattern=store.RUN_ID_RE.pattern)
    event_type: str = Field(pattern=_EVENT_TYPE)
    ts: float | None = None
    payload: dict = Field(default_factory=dict)


class TraceBatchIn(BaseModel):
    batch_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(pattern=store.RUN_ID_RE.pattern)
    spans: list[dict] = Field(min_length=1, max_length=trace_store.MAX_SPANS_PER_BATCH)
    schema_version: int = Field(default=config.TRACE_SCHEMA_VERSION, ge=1, le=10)
    final: bool = False


def _log_rejection(event_type, exc: Exception, scope: str) -> None:
    for item in rejection_diagnostics(event_type, exc, scope):
        log.warning("event rejected: event_type=%s field=%s error_type=%s", item["event_type"], item["field"],
                    item["error_type"])


def ingest_event(event: EventIn) -> str:
    conn = store.connect()
    try:
        if store.count_events(conn, event.run_id) >= config.EVENT_MAX_PER_RUN:
            raise HTTPException(429, "event limit reached for this run")
        try:
            inserted = store.add_event(conn, event.run_id, event.event_id, event.event_type, event.payload, event.ts)
        except store.EventExpired:
            raise HTTPException(410, "run is no longer accepting events")
        except store.EventRejected:
            raise HTTPException(403, "run is no longer accepting events")
        return "accepted" if inserted else "duplicate"
    finally:
        conn.close()


@ingress_router.post("/api/events")
async def receive_event(request: Request):
    if not config.get_event_secret():
        raise HTTPException(503, "Event ingest is not configured")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.EVENT_MAX_BYTES:
        raise HTTPException(413, "event too large")
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        raise HTTPException(415, "content type must be application/json")
    body = await request.body()
    if len(body) > config.EVENT_MAX_BYTES:
        raise HTTPException(413, "event too large")
    try:
        raw = json.loads(body)
        run_id = raw["run_id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "malformed event")
    if not isinstance(run_id, str) or not re.match(store.RUN_ID_RE.pattern, run_id):
        raise HTTPException(400, "malformed event")
    conn = store.connect()
    try:
        run = store.get_run(conn, run_id)
    finally:
        conn.close()
    version = int((run or {}).get("event_key_version") or config.EVENT_KEY_VERSION)
    if not demo_auth.verify_event(run_id, request.headers.get("x-aob-timestamp"), request.headers.get("x-aob-signature"),
                                  body, version):
        raise HTTPException(401, "invalid event signature")
    if run is None:
        raise HTTPException(401, "invalid event signature")
    try:
        event = EventIn.model_validate(raw)
    except ValidationError as exc:
        _log_rejection(raw.get("event_type"), exc, "envelope")
        raise HTTPException(422, "invalid event")
    try:
        event.payload = validate_payload(event.event_type, event.payload)
    except (ValidationError, ValueError) as exc:
        _log_rejection(event.event_type, exc, "payload")
        raise HTTPException(422, "invalid event")
    return {"status": await run_in_threadpool(ingest_event, event)}


@ingress_router.post("/api/traces", status_code=202)
async def receive_trace_batch(request: Request):
    """Receive a bounded, allowlisted post-run trace batch."""
    if not config.get_event_secret():
        raise HTTPException(503, "Trace ingest is not configured")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.TRACE_MAX_BATCH_BYTES:
        raise HTTPException(413, "trace batch too large")
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        raise HTTPException(415, "content type must be application/json")
    body = await request.body()
    if len(body) > config.TRACE_MAX_BATCH_BYTES:
        raise HTTPException(413, "trace batch too large")
    try:
        raw = json.loads(body)
        run_id = raw["run_id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "malformed trace batch")
    if not isinstance(run_id, str) or not store.RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(400, "malformed trace batch")
    conn = store.connect()
    try:
        run = store.get_run(conn, run_id)
        batches = conn.execute("SELECT COUNT(*) FROM trace_batches WHERE run_id=?", (run_id,)).fetchone()[0]
    finally:
        conn.close()
    version = int((run or {}).get("event_key_version") or config.EVENT_KEY_VERSION)
    if not demo_auth.verify_event(run_id, request.headers.get("x-aob-timestamp"),
                                  request.headers.get("x-aob-signature"), body, version):
        raise HTTPException(401, "invalid trace signature")
    if run is None:
        raise HTTPException(401, "invalid trace signature")
    if batches >= config.TRACE_MAX_BATCHES_PER_RUN:
        raise HTTPException(429, "trace batch limit reached for this run")
    conn = store.connect()
    try:
        if not store.trace_accepting(conn, run_id):
            raise HTTPException(410, "run is no longer accepting traces")
        try:
            batch = TraceBatchIn.model_validate(raw)
            result = trace_store.ingest(conn, batch.run_id, batch.batch_id, batch.spans,
                                        batch.schema_version, batch.final)
        except (ValidationError, trace_store.TraceValidationError) as exc:
            raise HTTPException(422, "invalid trace batch") from exc
        return result
    finally:
        conn.close()


@private_router.get("/api/demo/{run_id}/trace-summary")
def trace_summary(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    run = owned_run(conn, run_id, user)
    return trace_store.summary(conn, run_id, run.get("units"))


@private_router.get("/api/demo/{run_id}/llm-calls")
def llm_calls(run_id: str, limit: int = Query(100, ge=1, le=250), offset: int = Query(0, ge=0),
              sort: Literal["start", "duration"] = "start", unit_id: str | None = None,
              worker_id: str | None = None, attempt: int | None = Query(None, ge=1),
              user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    owned_run(conn, run_id, user)
    return trace_store.calls(conn, run_id, limit, offset, sort, unit_id, worker_id, attempt)


@private_router.get("/api/demo/{run_id}/units/{unit_id}/trace")
def unit_trace(run_id: str, unit_id: str,
               user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    owned_run(conn, run_id, user)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", unit_id):
        raise HTTPException(422, "invalid unit id")
    return trace_store.unit_trace(conn, run_id, unit_id)
