"""FastAPI service: read-only dashboard endpoints plus load-test and synthetic submission."""
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

import config
import demo_api
from api import queries
from intake.service import submit_unit
from loadtest import runner
from loadtest.metrics import run_metrics
from storage import db, files
from workers.model_client import make_client

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_model_cache: dict = {"at": 0.0, "value": None}
_producers: dict[str, threading.Thread] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_schema(conn)
    conn.close()
    yield


app = FastAPI(title="Agent Ops Board API", version="1.0", lifespan=lifespan)
app.include_router(demo_api.router)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def valid_id(value: str) -> str:
    if not _ID.match(value):
        raise HTTPException(404, "not found")
    return value


def model_info() -> dict:
    """Configured model; server version is probed at most once a minute."""
    if time.time() - _model_cache["at"] > 60:
        client = make_client()
        _model_cache.update(at=time.time(), value={
            "backend": client.backend, "name": client.model, "simulated": client.simulated,
            "server": client.server_version() if not client.simulated else None, "url": config.MODEL_URL})
    return _model_cache["value"]


def supervisor_status(conn) -> dict:
    beat = db.get_setting(conn, "supervisor_heartbeat")
    age = db.now() - float(beat) if beat else None
    return {"alive": age is not None and age < 10, "heartbeat_age_seconds": round(age, 1) if age is not None else None,
            "pid": db.get_setting(conn, "supervisor_pid"), "db_path": str(config.DB_PATH)}


@app.get("/v1/dashboard")
def dashboard(test_run_id: str | None = None, conn=Depends(get_conn)):
    run = queries.latest_run(conn, valid_id(test_run_id) if test_run_id else None)
    now = db.now()
    if run and run["state"] == "RUNNING":
        runner.finalize_if_done(conn, run["test_run_id"])
        run = queries.latest_run(conn, run["test_run_id"])
    return {"server_time": now, "scope": "run" if run else "all", "model": model_info(),
            "run": queries.run_view(run, now) if run else None,
            "metrics": run_metrics(conn, run, now), "workers": queries.list_workers(conn),
            "supervisor": supervisor_status(conn), "synthetic": True}


@app.get("/v1/workers")
def workers(conn=Depends(get_conn)):
    return {"server_time": db.now(), "items": queries.list_workers(conn)}


@app.get("/v1/workers/{worker_id}")
def worker(worker_id: str, conn=Depends(get_conn)):
    view = queries.get_worker(conn, valid_id(worker_id))
    if view is None:
        raise HTTPException(404, "worker not found")
    return {"server_time": db.now(), **view}


@app.get("/v1/workers/{worker_id}/events")
def worker_events(worker_id: str, limit: int = Query(100, ge=1, le=500), conn=Depends(get_conn)):
    return {"items": queries.worker_events(conn, valid_id(worker_id), limit)}


@app.get("/v1/intake-units")
def intake_units(status: str | None = None, channel: str | None = None, worker_id: str | None = None,
                 q: str | None = Query(None, max_length=64), intent: str | None = None,
                 test_run_id: str | None = None, created_after: float | None = None,
                 created_before: float | None = None, page: int = Query(1, ge=1),
                 page_size: int = Query(50, ge=1, le=200), conn=Depends(get_conn)):
    return queries.list_units(conn, status, channel, worker_id, q, intent, test_run_id,
                              created_after, created_before, page, page_size)


@app.get("/v1/intake-units/{unit_id}")
def intake_unit(unit_id: str, conn=Depends(get_conn)):
    unit = queries.get_unit(conn, valid_id(unit_id))
    if unit is None:
        raise HTTPException(404, "intake unit not found")
    return unit


@app.get("/v1/intake-units/{unit_id}/steps")
def intake_steps(unit_id: str, conn=Depends(get_conn)):
    valid_id(unit_id)
    job = conn.execute("SELECT attempt FROM intake_jobs WHERE unit_id=?", (unit_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "intake unit not found")
    attempts = [{"attempt": a, "steps": queries.unit_steps(conn, unit_id, a)}
                for a in range(1, max(job["attempt"], 1) + 1)]
    events = [{**dict(r), "details": None} for r in conn.execute(
        "SELECT worker_id, step_name, event_type, attempt, timestamp, duration_ms, error_code, message "
        "FROM worker_events WHERE unit_id=? ORDER BY id", (unit_id,))]
    return {"unit_id": unit_id, "attempts": attempts, "events": events}


@app.get("/v1/intake-units/{unit_id}/handoff")
def intake_handoff(unit_id: str, conn=Depends(get_conn)):
    valid_id(unit_id)
    if conn.execute("SELECT 1 FROM intake_units WHERE unit_id=?", (unit_id,)).fetchone() is None:
        raise HTTPException(404, "intake unit not found")
    return {"unit_id": unit_id, "handoff": queries.handoff_view(conn, unit_id)}


@app.get("/v1/intake-units/{unit_id}/attachments/{attachment_id}")
def attachment(unit_id: str, attachment_id: str, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM attachments WHERE unit_id=? AND attachment_id=?",
                       (valid_id(unit_id), valid_id(attachment_id))).fetchone()
    if row is None:
        raise HTTPException(404, "attachment not found")
    return Response(files.read_attachment(row["path"]), media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{attachment_id}.txt"',
                             "X-Content-Type-Options": "nosniff"})


class LoadTestRequest(BaseModel):
    scenario: Literal["multi_channel_350", "burst_350"] = "multi_channel_350"
    target_units: int = Field(350, ge=1, le=5000)
    arrival_window_seconds: float = Field(600, ge=0, le=7200)
    completion_deadline_seconds: float = Field(600, gt=0, le=7200)
    worker_count: int = Field(2, ge=1, le=8)
    inject_faults: bool | dict[str, int] = False


@app.post("/v1/load-tests", status_code=202)
def start_load_test(req: LoadTestRequest, conn=Depends(get_conn)):
    faults = runner.DEMO_FAULTS if req.inject_faults is True else (req.inject_faults or None)
    try:
        run_id = runner.create_run(conn, req.scenario, req.target_units, req.arrival_window_seconds,
                                   req.completion_deadline_seconds, req.worker_count, faults or None)
    except runner.Conflict as exc:
        raise HTTPException(409, str(exc))
    _producers[run_id] = runner.start_producer(run_id)
    return {"test_run_id": run_id, "state": "RUNNING"}


@app.get("/v1/load-tests")
def list_load_tests(conn=Depends(get_conn)):
    now = db.now()
    rows = conn.execute("SELECT * FROM load_test_runs ORDER BY started_at DESC LIMIT 20").fetchall()
    return {"items": [queries.run_view(dict(r), now) for r in rows], "scenarios": runner.SCENARIOS}


@app.get("/v1/load-tests/{run_id}")
def load_test(run_id: str, conn=Depends(get_conn)):
    run = queries.latest_run(conn, valid_id(run_id))
    if run is None:
        raise HTTPException(404, "load test not found")
    if run["state"] == "RUNNING":
        runner.finalize_if_done(conn, run_id)
        run = queries.latest_run(conn, run_id)
    now = db.now()
    return {**queries.run_view(run, now), "metrics": run_metrics(conn, run, now),
            "model": model_info()}


@app.post("/v1/load-tests/{run_id}/stop")
def stop_load_test(run_id: str, conn=Depends(get_conn)):
    if queries.latest_run(conn, valid_id(run_id)) is None:
        raise HTTPException(404, "load test not found")
    runner.request_stop(conn, run_id)
    return {"test_run_id": run_id, "stop_requested": True}


class MessageRequest(BaseModel):
    channel: Literal["email", "chat", "voice"]
    subject: str = Field("", max_length=300)
    body: str = Field("", max_length=config.MAX_TEXT_CHARS)
    messages: list[dict] = Field(default_factory=list, max_length=200)

    def payload(self) -> dict:
        if self.channel == "email":
            return {"subject": self.subject, "body": self.body}
        key = "messages" if self.channel == "chat" else "segments"
        return {key: [{"speaker": str(m.get("speaker", "")), "text": str(m.get("text", "")),
                       "timestamp": m.get("timestamp"), "start_s": m.get("start_s")} for m in self.messages]}


@app.post("/v1/messages", status_code=202)
def submit_message(req: MessageRequest, conn=Depends(get_conn)):
    try:
        unit_id = submit_unit(conn, req.channel, req.payload())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"unit_id": unit_id, "status": "PENDING"}
