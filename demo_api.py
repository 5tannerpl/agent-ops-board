"""Live-demo HTTP API: Auth0-protected start/status/events plus the signed Kaggle event ingest."""
import json
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

router = APIRouter()
_EVENT_TYPE = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
_EVENT_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


def get_conn():
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


def run_view(run: dict) -> dict:
    return {"run_id": run["run_id"], "status": run["status"], "kaggle_status": run["kaggle_status"],
            "error": run["error"], "mode": run.get("mode"), "units": run.get("units"),
            "created_at": run["created_at"], "updated_at": run["updated_at"], "finished_at": run["finished_at"]}


def owned_run(conn, run_id: str, user: dict) -> dict:
    """404 (not 403) for other users' runs so run ids cannot be probed."""
    run = store.get_run(conn, run_id) if store.RUN_ID_RE.match(run_id) else None
    if run is None or run["owner_sub"] != user["sub"]:
        raise HTTPException(404, "run not found")
    return demo_service.sync_run(conn, run)


@router.get("/api/me")
def me(user: dict = Depends(demo_auth.require_demo_access)):
    return {"user_id": user["sub"], "can_start_demo": True}


class StartIn(BaseModel):
    mode: Literal["light", "high_concurrency"] = "light"
    units: Literal[50, 100, 150] | None = None  # high_concurrency only; light always uses its fixed size


@router.post("/api/demo/start", status_code=202)
def start_demo(background: BackgroundTasks, body: StartIn | None = None,
               user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    body = body or StartIn()
    if body.mode == "light" and body.units is not None:
        raise HTTPException(422, "units can only be chosen for high_concurrency")
    units = body.units or (100 if body.mode == "high_concurrency" else 10)
    try:
        run = demo_service.start_run(conn, user["sub"], body.mode, units)
    except store.Conflict as exc:
        raise HTTPException(409, "A demo is already queued or running", headers={"X-Active-Run": exc.run_id})
    except store.RateLimited as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)})
    background.add_task(demo_service.submit_run, run["run_id"])
    return run_view(run)


@router.get("/api/demo/runs")
def list_runs(user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    runs = [demo_service.sync_run(conn, r) for r in store.list_runs(conn, user["sub"])]
    return {"items": [run_view(r) for r in runs]}


@router.get("/api/demo/{run_id}/status")
def run_status(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    run = owned_run(conn, run_id, user)
    return {**run_view(run), **demo_service.summarize_run(conn, run)}


@router.get("/api/demo/{run_id}/state")
def run_state(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    run = owned_run(conn, run_id, user)
    return {**run_view(run), **demo_scaling.state(conn, run)}


@router.get("/api/demo/{run_id}/metrics")
def run_metrics(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    return demo_scaling.metrics(conn, owned_run(conn, run_id, user))


@router.get("/api/demo/{run_id}/summary")
def run_summary(run_id: str, user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    return demo_scaling.summary(conn, owned_run(conn, run_id, user))


@router.get("/api/demo/{run_id}/events")
def run_events(run_id: str, after_id: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500),
               types: str | None = Query(None, max_length=1000, description="comma-separated event types"),
               user: dict = Depends(demo_auth.require_demo_access), conn=Depends(get_conn)):
    owned_run(conn, run_id, user)
    wanted = tuple(t for t in (types or "").split(",") if re.match(_EVENT_TYPE, t)) or None
    return {"items": store.list_events(conn, run_id, after_id, limit, wanted)}


class EventIn(BaseModel):
    event_id: str = Field(pattern=_EVENT_ID)
    run_id: str = Field(pattern=store.RUN_ID_RE.pattern)
    event_type: str = Field(pattern=_EVENT_TYPE)
    ts: float | None = None
    payload: dict = Field(default_factory=dict)


def ingest_event(event: EventIn) -> str:
    conn = store.connect()
    try:
        if store.get_run(conn, event.run_id) is None:
            raise HTTPException(404, "unknown run")
        if store.count_events(conn, event.run_id) >= config.EVENT_MAX_PER_RUN:
            raise HTTPException(429, "event limit reached for this run")
        inserted = store.add_event(conn, event.run_id, event.event_id, event.event_type, event.payload, event.ts)
        return "accepted" if inserted else "duplicate"
    finally:
        conn.close()


@router.post("/api/events")
async def receive_event(request: Request):
    """Authenticated by an HMAC over the raw body with a per-run key, not by an Auth0 token."""
    if not config.EVENT_SECRET:
        raise HTTPException(503, "Event ingest is not configured")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.EVENT_MAX_BYTES:
        raise HTTPException(413, "event too large")
    body = await request.body()
    if len(body) > config.EVENT_MAX_BYTES:
        raise HTTPException(413, "event too large")
    try:
        raw = json.loads(body)
        run_id = raw["run_id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "malformed event")
    if not isinstance(run_id, str) or not re.match(store.RUN_ID_RE.pattern, run_id) or not demo_auth.verify_event(
            run_id, request.headers.get("x-aob-timestamp"), request.headers.get("x-aob-signature"), body):
        raise HTTPException(401, "invalid event signature")
    try:
        event = EventIn.model_validate(raw)
    except ValidationError:
        raise HTTPException(422, "invalid event")
    return {"status": await run_in_threadpool(ingest_event, event)}
