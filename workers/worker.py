"""Worker: claims jobs, runs the six-step workflow with durable checkpoints, heartbeats its lease."""
import json
import os
import signal
import socket
import threading
import time

import config
from intake import schema
from intake.service import load_segments
from orchestration import handoff
from storage import db, files
from workers import model_client
from workers.queue import (Job, LeaseLost, assert_lease, claim_job, complete_job, fail_or_retry,
                           log_event, save_checkpoint, send_to_review)


class SimulatedCrash(BaseException):
    """Injected fault: the worker dies without any cleanup (no release, no final writes)."""


class InvalidModelOutput(Exception):
    code = "INVALID_MODEL_OUTPUT"


class HandoffFailed(handoff.HandoffError):
    code = "HANDOFF_FAILED"


def _code(exc: BaseException) -> str:
    return getattr(exc, "code", "INTERNAL_ERROR")


class Worker:
    def __init__(self, worker_id: str, client=None):
        self.worker_id = worker_id
        self.client = client or model_client.make_client()
        self.conn = db.connect()
        self.stop_event = threading.Event()
        self.lease_lost = threading.Event()
        self._hb_stop = threading.Event()
        self._in_backoff = False
        self._job: Job | None = None
        self._lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------
    def register(self) -> None:
        now = db.now()
        self.conn.execute(
            "INSERT INTO workers(worker_id, status, pid, host, model_name, started_at, last_heartbeat) "
            "VALUES(?, 'IDLE', ?, ?, ?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET status='IDLE', "
            "current_unit_id=NULL, current_step=NULL, step_started_at=NULL, backoff_until=NULL, pid=excluded.pid, "
            "host=excluded.host, model_name=excluded.model_name, started_at=excluded.started_at, "
            "last_heartbeat=excluded.last_heartbeat",
            (self.worker_id, os.getpid(), socket.gethostname(), self.client.model, now, now))
        log_event(self.conn, self.worker_id, None, None, "WORKER_STARTED", message=f"pid {os.getpid()}")

    def _heartbeat_loop(self) -> None:
        conn = db.connect()
        while not self._hb_stop.wait(config.HEARTBEAT_SECONDS):
            try:
                now = db.now()
                conn.execute("UPDATE workers SET last_heartbeat=? WHERE worker_id=? AND status!='OFFLINE'",
                             (now, self.worker_id))
                with self._lock:
                    job = self._job
                if job:
                    cur = conn.execute(
                        "UPDATE intake_jobs SET lease_until=? WHERE job_id=? AND status='RUNNING' "
                        "AND lease_owner=? AND lease_version=?",
                        (now + config.LEASE_SECONDS, job.job_id, self.worker_id, job.lease_version))
                    if cur.rowcount == 0:
                        self.lease_lost.set()
            except Exception:
                pass  # transient DB contention; the next beat retries
        conn.close()

    def run(self) -> str:
        self.register()
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()
        try:
            while not self.stop_event.is_set():
                if self._backoff_active():
                    continue
                job = claim_job(self.conn, self.worker_id)
                if job is None:
                    self.stop_event.wait(config.POLL_SECONDS)
                    continue
                with self._lock:
                    self._job = job
                self.lease_lost.clear()
                try:
                    self._process(job)
                except LeaseLost:
                    log_event(self.conn, self.worker_id, job.unit_id, None, "LEASE_LOST", job.attempt,
                              error_code="LEASE_LOST", message="lease reassigned; discarding work")
                except SimulatedCrash:
                    raise
                except Exception as exc:  # bookkeeping failure; the lease will expire and the job recovers
                    log_event(self.conn, self.worker_id, job.unit_id, None, "WORKER_ERROR", job.attempt,
                              error_code=_code(exc), message=str(exc))
                with self._lock:
                    self._job = None
                self._go_idle()
        except SimulatedCrash:
            self._hb_stop.set()  # a crashed process leaves lease, step and worker rows untouched
            return "crashed"
        self._hb_stop.set()
        self.conn.execute("UPDATE workers SET status='OFFLINE', current_unit_id=NULL, current_step=NULL "
                          "WHERE worker_id=?", (self.worker_id,))
        log_event(self.conn, self.worker_id, None, None, "WORKER_STOPPED")
        return "stopped"

    def _go_idle(self) -> None:
        self.conn.execute(
            "UPDATE workers SET status=CASE WHEN backoff_until>? THEN 'BACKOFF' ELSE 'IDLE' END, "
            "current_unit_id=NULL, current_step=NULL, step_started_at=NULL WHERE worker_id=?",
            (db.now(), self.worker_id))

    def _backoff_active(self) -> bool:
        row = self.conn.execute("SELECT backoff_until FROM workers WHERE worker_id=?", (self.worker_id,)).fetchone()
        if row and row["backoff_until"] and row["backoff_until"] > db.now():
            if not self._in_backoff:
                self.conn.execute("UPDATE workers SET status='BACKOFF' WHERE worker_id=?", (self.worker_id,))
                self._in_backoff = True
            self.stop_event.wait(0.5)
            return True
        if self._in_backoff:
            self.conn.execute("UPDATE workers SET status='IDLE', backoff_until=NULL WHERE worker_id=?",
                              (self.worker_id,))
            self._in_backoff = False
        return False

    # ---- step plumbing ---------------------------------------------------
    def _step(self, job: Job, name: str, fn=None, skip: str | None = None):
        conn, now = self.conn, db.now()
        if self.lease_lost.is_set():
            raise LeaseLost(job.unit_id)
        if skip:
            with db.tx(conn):
                assert_lease(conn, job)
                conn.execute(
                    "INSERT OR REPLACE INTO step_attempts(unit_id, step_name, attempt, status, worker_id, "
                    "started_at, finished_at, error_message) VALUES(?,?,?,'SKIPPED',?,?,?,?)",
                    (job.unit_id, name, job.attempt, self.worker_id, now, now, skip))
                log_event(conn, self.worker_id, job.unit_id, name, "STEP_SKIPPED", job.attempt, message=skip)
            return None
        with db.tx(conn):
            assert_lease(conn, job)
            conn.execute(
                "INSERT OR REPLACE INTO step_attempts(unit_id, step_name, attempt, status, worker_id, started_at) "
                "VALUES(?,?,?,'RUNNING',?,?)", (job.unit_id, name, job.attempt, self.worker_id, now))
            conn.execute("UPDATE intake_units SET current_step=? WHERE unit_id=?", (name, job.unit_id))
            conn.execute("UPDATE workers SET current_step=?, step_started_at=? WHERE worker_id=?",
                         (name, now, self.worker_id))
            log_event(conn, self.worker_id, job.unit_id, name, "STEP_STARTED", job.attempt)
        t0 = time.perf_counter()
        try:
            result = fn()
        except (LeaseLost, SimulatedCrash):
            raise
        except BaseException as exc:
            ms = (time.perf_counter() - t0) * 1000
            with db.tx(conn):
                conn.execute(
                    "UPDATE step_attempts SET status='FAILED', finished_at=?, error_code=?, error_message=? "
                    "WHERE unit_id=? AND step_name=? AND attempt=?",
                    (db.now(), _code(exc), str(exc)[:300], job.unit_id, name, job.attempt))
                log_event(conn, self.worker_id, job.unit_id, name, "STEP_FAILED", job.attempt, ms,
                          _code(exc), str(exc))
            raise
        ms = (time.perf_counter() - t0) * 1000
        with db.tx(conn):
            conn.execute("UPDATE step_attempts SET status='COMPLETED', finished_at=? "
                         "WHERE unit_id=? AND step_name=? AND attempt=?",
                         (db.now(), job.unit_id, name, job.attempt))
            log_event(conn, self.worker_id, job.unit_id, name, "STEP_COMPLETED", job.attempt, ms)
        return result

    def _inject(self, job: Job, fault: str | None, kind: str) -> bool:
        if fault == kind and job.attempt == 1:
            log_event(self.conn, self.worker_id, job.unit_id, None, "INJECTED_FAULT", job.attempt,
                      error_code=kind, message="controlled fault injection (demo)")
            return True
        return False

    # ---- workflow --------------------------------------------------------
    def _process(self, job: Job) -> None:
        conn = self.conn
        unit = conn.execute("SELECT channel, fault FROM intake_units WHERE unit_id=?", (job.unit_id,)).fetchone()
        fault, channel = unit["fault"], unit["channel"]
        saved = conn.execute("SELECT 1 FROM intake_results WHERE unit_id=? AND schema_version=?",
                             (job.unit_id, config.SCHEMA_VERSION)).fetchone() is not None
        ctx: dict = {}
        try:
            if saved:
                for name in config.STEPS[:-1]:
                    self._step(job, name, skip="result already persisted (checkpoint)")
            else:
                self._step(job, "NORMALIZE_INPUT", lambda: self._normalize(job, ctx, fault))
                self._step(job, "LOAD_CONTEXT", lambda: ctx.update(
                    prompt=schema.build_user_prompt(ctx["segments"], channel)))
                if "llm_output" in job.checkpoint:
                    self._step(job, "LLM_INFERENCE", skip="model output restored from checkpoint")
                else:
                    self._step(job, "LLM_INFERENCE", lambda: self._infer(job, ctx, fault))
                self._step(job, "VALIDATE_OUTPUT", lambda: self._validate(job, ctx, channel))
                self._step(job, "SAVE_RESULT", lambda: self._save(job, ctx, channel))
            self._step(job, "HANDOFF", lambda: self._handoff(job, fault))
            complete_job(conn, job)
        except (LeaseLost, SimulatedCrash):
            raise
        except files.CorruptInput as exc:
            send_to_review(conn, job, "CORRUPT_INPUT", str(exc))
        except InvalidModelOutput as exc:
            send_to_review(conn, job, exc.code, str(exc))
        except Exception as exc:
            code = _code(exc)
            if isinstance(exc, model_client.ResourceExhausted):
                until = db.now() + config.RESOURCE_BACKOFF_SECONDS
                conn.execute("UPDATE workers SET backoff_until=? WHERE worker_id=?", (until, self.worker_id))
            fail_or_retry(conn, job, code, str(exc) or code, job_max(conn, job))

    def _normalize(self, job: Job, ctx: dict, fault: str | None) -> None:
        if fault == "CORRUPT_INPUT":
            raise files.CorruptInput("injected corrupt input")
        ctx["segments"] = load_segments(self.conn, job.unit_id)

    def _infer(self, job: Job, ctx: dict, fault: str | None) -> None:
        if self._inject(job, fault, "WORKER_CRASH_ONCE"):
            raise SimulatedCrash()
        if self._inject(job, fault, "LLM_TIMEOUT_ONCE"):
            raise model_client.LLMTimeout("injected model timeout")
        if self._inject(job, fault, "RESOURCE_EXHAUSTED_ONCE"):
            raise model_client.ResourceExhausted("injected out-of-memory")
        out = self.client.generate(schema.SYSTEM_PROMPT, ctx["prompt"], config.MODEL_TIMEOUT)
        if self._inject(job, fault, "INVALID_JSON_ONCE"):
            out = "The customer seems to want a payment plan."
        save_checkpoint(self.conn, job, {"llm_output": out})

    def _validate(self, job: Job, ctx: dict, channel: str) -> None:
        out = job.checkpoint["llm_output"]
        failures = 0
        try:
            result = schema.validate_output(out, ctx["segments"])
        except schema.InvalidOutput as first:
            failures = 1
            log_event(self.conn, self.worker_id, job.unit_id, "VALIDATE_OUTPUT", "VALIDATION_FAILED", job.attempt,
                      error_code="INVALID_MODEL_OUTPUT", message=str(first))
            repaired = self.client.generate(
                schema.SYSTEM_PROMPT, schema.build_repair_prompt(ctx["prompt"], out, str(first)), config.MODEL_TIMEOUT)
            save_checkpoint(self.conn, job, {"llm_output": repaired})
            try:
                result = schema.validate_output(repaired, ctx["segments"])
            except schema.InvalidOutput as second:
                raise InvalidModelOutput(f"invalid after one repair: {second}") from second
        result["validation"].update(parse_failures=failures, repair_attempts=failures)
        ctx["result"] = result

    def _save(self, job: Job, ctx: dict, channel: str) -> None:
        result = ctx["result"]
        result.update(unit_id=job.unit_id, channel=channel)
        targets = handoff.propose_targets(result)
        event_id = f"HO-{job.unit_id}-{config.SCHEMA_VERSION}"
        payload = {"event_id": event_id, "unit_id": job.unit_id, "targets": targets,
                   "intents": [i["label"] for i in result["intents"]], "schema_version": config.SCHEMA_VERSION}
        model_version = f"{self.client.backend}:{self.client.model}"
        now = db.now()
        with db.tx(self.conn) as c:  # result and outbox event commit atomically
            assert_lease(c, job)
            c.execute("INSERT OR IGNORE INTO intake_results VALUES(?,?,?,?,?)",
                      (job.unit_id, config.SCHEMA_VERSION, model_version, json.dumps(result), now))
            c.execute("DELETE FROM intent_evidence WHERE unit_id=?", (job.unit_id,))
            for intent in result["intents"]:
                for ev in intent["evidence"]:
                    c.execute(
                        "INSERT INTO intent_evidence(unit_id, label, source_message_id, start_offset, end_offset, "
                        "quote, verified) VALUES(?,?,?,?,?,?,?)",
                        (job.unit_id, intent["label"], ev["source_message_id"], ev["start_offset"],
                         ev["end_offset"], ev["quote"], int(ev["verified"])))
            c.execute("INSERT OR IGNORE INTO handoff_outbox(event_id, unit_id, payload, status, created_at) "
                      "VALUES(?,?,?, 'PENDING', ?)", (event_id, job.unit_id, json.dumps(payload), now))

    def _handoff(self, job: Job, fault: str | None) -> None:
        event_id = f"HO-{job.unit_id}-{config.SCHEMA_VERSION}"
        row = self.conn.execute("SELECT payload, status FROM handoff_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise HandoffFailed("outbox event missing")
        if row["status"] == "DELIVERED":
            return
        try:
            if self._inject(job, fault, "HANDOFF_FAIL_ONCE"):
                raise HandoffFailed("mock orchestrator unavailable (injected)")
            handoff.deliver(self.conn, event_id, job.unit_id, row["payload"])
        except Exception as exc:
            final = job.attempt >= job_max(self.conn, job)
            self.conn.execute(
                "UPDATE handoff_outbox SET attempts=attempts+1, last_error=?, status=? WHERE event_id=?",
                (str(exc)[:300], "FAILED" if final else "PENDING", event_id))
            if isinstance(exc, HandoffFailed):
                raise
            raise HandoffFailed(str(exc)) from exc
        with db.tx(self.conn) as c:
            assert_lease(c, job)
            c.execute("UPDATE handoff_outbox SET status='DELIVERED', attempts=attempts+1, delivered_at=?, "
                      "last_error=NULL WHERE event_id=?", (db.now(), event_id))


def job_max(conn, job: Job) -> int:
    return conn.execute("SELECT max_attempts FROM intake_jobs WHERE job_id=?", (job.job_id,)).fetchone()[0]


def main(worker_id: str) -> None:
    conn = db.connect()
    db.init_schema(conn)
    conn.close()
    worker = Worker(worker_id)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stop_event.set())
    outcome = worker.run()
    if outcome == "crashed":
        os._exit(70)


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "worker-01")
