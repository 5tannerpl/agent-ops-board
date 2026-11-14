import threading
import time

import pytest
from fastapi.testclient import TestClient

import config
from api.main import app
from loadtest import runner
from storage import db
from workers.worker import Worker


@pytest.fixture
def pool(env):
    workers = [Worker(f"worker-0{i}") for i in (1, 2)]
    threads = [threading.Thread(target=w.run, daemon=True) for w in workers]
    for t in threads:
        t.start()
    yield workers
    for w in workers:
        w.stop_event.set()
    for t in threads:
        t.join(timeout=5)


def test_burst_with_faults_completes_and_writes_results(env, pool):
    conn = env
    run_id = runner.create_run(conn, "burst_350", 24, 0, 60, 2, runner.DEMO_FAULTS)
    with pytest.raises(runner.Conflict):  # no parallel runs
        runner.create_run(conn, "burst_350", 5, 0, 60, 2)
    runner.produce(run_id)  # produces then blocks until the run is finalised
    run = dict(conn.execute("SELECT * FROM load_test_runs WHERE test_run_id=?", (run_id,)).fetchone())
    assert run["state"] == "FINISHED"
    assert conn.execute("SELECT COUNT(*) FROM intake_units WHERE status='COMPLETED'").fetchone()[0] == 24
    assert (config.RESULTS_DIR / run_id / "performance.csv").exists()
    assert conn.execute("SELECT COUNT(*) FROM intake_jobs WHERE attempt>1").fetchone()[0] >= 3  # faults retried


def test_api_read_endpoints_and_validation(env, pool):
    client = TestClient(app)
    r = client.post("/v1/messages", json={"channel": "chat", "messages": [
        {"speaker": "Agent", "text": "Hello"}, {"speaker": "Customer", "text": "I dispute the balance."}]})
    assert r.status_code == 202
    uid = r.json()["unit_id"]
    for _ in range(100):
        unit = client.get(f"/v1/intake-units/{uid}").json()
        if unit["status"] == "COMPLETED":
            break
        time.sleep(0.1)
    assert unit["status"] == "COMPLETED"
    ev = unit["intents"][0]["evidence"][0]
    seg = next(s for s in unit["segments"] if s["id"] == ev["source_message_id"])
    assert seg["text"][ev["start_offset"]:ev["end_offset"]] == ev["quote"] == "I dispute the balance"
    assert [s["status"] for s in client.get(f"/v1/intake-units/{uid}/steps").json()["attempts"][-1]["steps"]] \
        == ["COMPLETED"] * 6
    assert client.get(f"/v1/intake-units/{uid}/handoff").json()["handoff"]["status"] == "DELIVERED"
    assert client.get("/v1/intake-units", params={"intent": "DEBT_DISPUTE", "channel": "chat"}).json()["total"] == 1
    assert client.get("/v1/workers").json()["items"]
    assert client.get("/v1/workers/worker-01/events").json()["items"]
    assert client.get("/v1/dashboard").json()["metrics"]["counts"]["completed"] == 1
    assert client.get("/v1/intake-units/..%2Fetc").status_code == 404
    assert client.get("/v1/intake-units/INT-9999").status_code == 404
    assert client.post("/v1/messages", json={"channel": "email", "body": "  "}).status_code == 422


def test_worker_health_derived_from_heartbeat(env):
    conn = env
    conn.execute("INSERT INTO workers(worker_id,status,last_heartbeat,current_unit_id) VALUES('w1','BUSY',?, 'INT-1')",
                 (db.now() - config.OFFLINE_AFTER - 5,))
    client = TestClient(app)
    assert client.get("/v1/workers").json()["items"][0]["state"] == "OFFLINE"
