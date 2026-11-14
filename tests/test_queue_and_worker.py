import json
import time

import pytest

import config
from intake.service import submit_unit
from storage import db
from workers import model_client
from workers.queue import LeaseLost, claim_job, fail_or_retry, save_checkpoint
from workers.worker import SimulatedCrash, Worker

EMAIL = {"subject": "Query", "body": "I already paid $2,000. I dispute the balance and request monthly instalments."}


def _worker(conn, wid="worker-01"):
    w = Worker(wid)
    w.register()
    return w


def test_claim_is_exclusive_and_expired_lease_is_fenced(env):
    conn = env
    submit_unit(conn, "email", EMAIL)
    a, b = db.connect(), db.connect()
    for wid, c in (("w-a", a), ("w-b", b)):
        c.execute("INSERT INTO workers(worker_id,status,last_heartbeat) VALUES(?, 'IDLE', ?)", (wid, db.now()))
    job_a = claim_job(a, "w-a", lease_seconds=0.2)
    assert job_a and claim_job(b, "w-b") is None  # single claim
    time.sleep(0.3)
    job_b = claim_job(b, "w-b")
    assert job_b and job_b.lease_version == job_a.lease_version + 1 and job_b.attempt == 2
    with pytest.raises(LeaseLost):  # stale worker may not write anymore
        save_checkpoint(a, job_a, {"llm_output": "stale"})


def test_retry_then_terminal_failure(env):
    conn = env
    uid = submit_unit(conn, "email", EMAIL)
    conn.execute("INSERT INTO workers(worker_id,status,last_heartbeat) VALUES('w', 'IDLE', 0)")
    statuses = []
    for _ in range(3):
        job = None
        while job is None:
            job = claim_job(conn, "w")
            time.sleep(0.05)
        statuses.append(fail_or_retry(conn, job, "LLM_TIMEOUT", "boom", config.MAX_ATTEMPTS))
    assert statuses == ["WAITING_RETRY", "WAITING_RETRY", "FAILED"]
    assert conn.execute("SELECT status FROM intake_units WHERE unit_id=?", (uid,)).fetchone()[0] == "FAILED"


def _run_one(conn, worker):
    job = claim_job(conn, worker.worker_id)
    worker._job = job
    worker._process(job)
    return job


def test_pipeline_persists_verified_evidence_and_handoff(env):
    conn = env
    uid = submit_unit(conn, "email", EMAIL)
    _run_one(conn, _worker(conn))
    assert conn.execute("SELECT status FROM intake_units WHERE unit_id=?", (uid,)).fetchone()[0] == "COMPLETED"
    rows = conn.execute("SELECT label, quote, start_offset, end_offset, verified FROM intent_evidence "
                        "WHERE unit_id=?", (uid,)).fetchall()
    assert {r["label"] for r in rows} == {"PAYMENT_VERIFICATION", "DEBT_DISPUTE", "PAYMENT_ARRANGEMENT"}
    assert all(r["verified"] and EMAIL["body"][r["start_offset"]:r["end_offset"]] == r["quote"] for r in rows)
    assert conn.execute("SELECT status FROM handoff_outbox").fetchone()[0] == "DELIVERED"
    assert conn.execute("SELECT COUNT(*) FROM orchestrator_inbox").fetchone()[0] == 1


def test_invalid_json_triggers_one_repair(env):
    conn = env
    uid = submit_unit(conn, "email", EMAIL, fault="INVALID_JSON_ONCE")
    _run_one(conn, _worker(conn))
    result = json.loads(conn.execute("SELECT result_json FROM intake_results WHERE unit_id=?", (uid,)).fetchone()[0])
    assert result["validation"]["repair_attempts"] == 1


def test_persistent_invalid_json_goes_to_manual_review(env, monkeypatch):
    conn = env
    uid = submit_unit(conn, "email", EMAIL)

    class Garbage(model_client.MockClient):
        def generate(self, system, user, timeout):
            return "nope"

    w = Worker("worker-01", client=Garbage())
    w.register()
    _run_one(conn, w)
    assert conn.execute("SELECT status FROM intake_units WHERE unit_id=?", (uid,)).fetchone()[0] == "MANUAL_REVIEW"


def test_crash_recovers_on_another_worker_and_resumes(env):
    conn = env
    uid = submit_unit(conn, "email", EMAIL, fault="WORKER_CRASH_ONCE")
    w1, w2 = _worker(conn, "worker-01"), _worker(conn, "worker-02")
    with pytest.raises(SimulatedCrash):
        _run_one(conn, w1)
    time.sleep(config.LEASE_SECONDS + 0.2)  # no heartbeat thread running: lease expires
    job = _run_one(conn, w2)
    assert job.attempt == 2
    assert conn.execute("SELECT status FROM intake_units WHERE unit_id=?", (uid,)).fetchone()[0] == "COMPLETED"
    stale = conn.execute("SELECT status, error_code FROM step_attempts WHERE unit_id=? AND attempt=1 "
                         "AND step_name='LLM_INFERENCE'", (uid,)).fetchone()
    assert tuple(stale) == ("FAILED", "LEASE_EXPIRED")
