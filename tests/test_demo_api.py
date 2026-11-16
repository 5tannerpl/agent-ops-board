import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import config
import demo_auth
import kaggle_launcher
import demo_service
import demo_store as store
from api.main import app

DOMAIN = "tenant.example.auth0.com"
AUDIENCE = "https://agent-ops-board-api"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(sub="auth0|alice", **overrides):
    claims = {"sub": sub, "iss": f"https://{DOMAIN}/", "aud": AUDIENCE, "exp": time.time() + 600, **overrides}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def auth(sub="auth0|alice", **overrides):
    return {"Authorization": f"Bearer {token(sub, **overrides)}"}


@pytest.fixture
def demo(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed_users.json"
    allowed.write_text(json.dumps({"allowed_users": {
        "auth0|alice": {"username": "alice", "enabled": True},
        "auth0|bob": {"username": "bob", "enabled": True},
        "auth0|off": {"username": "off", "enabled": False}}}), encoding="utf-8")
    monkeypatch.setattr(config, "AUTH0_DOMAIN", DOMAIN)
    monkeypatch.setattr(config, "AUTH0_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(config, "ALLOWED_USERS_FILE", allowed)
    monkeypatch.setattr(config, "DEMO_DB_PATH", tmp_path / "demo.db")
    monkeypatch.setattr(config, "EVENT_SECRET", "test-secret")
    monkeypatch.setattr(config, "EVENTS_PUBLIC_URL", "https://demo.example.com/api/events")
    monkeypatch.setattr(config, "DEMO_MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(demo_auth, "signing_key", lambda _t: KEY.public_key())
    pushed, status = [], {"value": "queued"}
    monkeypatch.setattr(kaggle_launcher, "submit", pushed.append)
    monkeypatch.setattr(kaggle_launcher, "kernel_status", lambda: status["value"])
    client = TestClient(app)
    client.pushed, client.kaggle = pushed, status
    return client


def signed_post(client, run_id, event_id="evt-00000001", event_type="model_validation_started",
                ts=None, key_run_id=None, **extra):
    body = json.dumps({"event_id": event_id, "run_id": run_id, "event_type": event_type,
                       "payload": {"seconds": 1.5}, **extra}).encode()
    stamp = str(int(ts or time.time()))
    sig = demo_auth.sign_event(key_run_id or run_id, stamp, body)
    return client.post("/api/events", content=body, headers={
        "X-AOB-Timestamp": stamp, "X-AOB-Signature": sig, "Content-Type": "application/json"})


def test_me_requires_valid_token_and_whitelist(demo):
    assert demo.get("/api/me").status_code == 401
    assert demo.get("/api/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert demo.get("/api/me", headers=auth(aud="https://other")).status_code == 401
    assert demo.get("/api/me", headers=auth(iss="https://evil/")).status_code == 401
    assert demo.get("/api/me", headers=auth(exp=time.time() - 60)).status_code == 401
    assert demo.get("/api/me", headers=auth("auth0|stranger")).status_code == 403
    assert demo.get("/api/me", headers=auth("auth0|off")).status_code == 403
    ok = demo.get("/api/me", headers=auth())
    assert ok.status_code == 200 and ok.json()["user_id"] == "auth0|alice"


def test_whitelist_change_applies_immediately_and_fails_closed(demo):
    assert demo.get("/api/me", headers=auth()).status_code == 200
    config.ALLOWED_USERS_FILE.write_text("{not json", encoding="utf-8")
    assert demo.get("/api/me", headers=auth()).status_code == 403
    config.ALLOWED_USERS_FILE.unlink()
    assert demo.get("/api/me", headers=auth()).status_code == 403


def test_start_is_exclusive_and_reports_real_status(demo):
    assert demo.post("/api/demo/start").status_code == 401
    assert demo.post("/api/demo/start", headers=auth("auth0|stranger")).status_code == 403
    first = demo.post("/api/demo/start", headers=auth())
    assert first.status_code == 202
    run_id = first.json()["run_id"]
    assert demo.pushed == [run_id]
    # The background submission already ran: accepted push = queued, not GPU ready.
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["status"] == "queued"
    again = demo.post("/api/demo/start", headers=auth("auth0|bob"))
    assert again.status_code == 409 and again.headers["x-active-run"] == run_id
    assert len(demo.pushed) == 1


def test_status_follows_kaggle_and_frees_the_slot(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    demo.kaggle["value"] = "running"
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["status"] == "running"
    demo.kaggle["value"] = "complete"
    conn = store.connect()
    conn.execute("UPDATE demo_runs SET last_synced_at=0")
    conn.close()
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["status"] == "complete"
    assert demo.post("/api/demo/start", headers=auth()).status_code == 202


def test_stale_terminal_status_right_after_push_is_ignored(demo):
    demo.kaggle["value"] = "complete"  # previous notebook version's result
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["status"] == "queued"


def test_failed_submission_marks_run_failed(demo, monkeypatch):
    def boom(_run_id):
        raise kaggle_launcher.LaunchError("kaggle kernels push failed: nope")
    monkeypatch.setattr(kaggle_launcher, "submit", boom)
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    status = demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()
    assert status["status"] == "failed" and "nope" in status["error"]


def test_rate_limit_per_user(demo, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MIN_INTERVAL_SECONDS", 300)
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    conn = store.connect()
    store.finish(conn, run_id, "complete")
    conn.close()
    limited = demo.post("/api/demo/start", headers=auth())
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    assert demo.post("/api/demo/start", headers=auth("auth0|bob")).status_code == 202


def test_stuck_runs_time_out(demo, monkeypatch):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    monkeypatch.setattr(config, "DEMO_MAX_RUN_SECONDS", -1)
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["status"] == "failed"


def test_events_are_signed_deduplicated_and_persisted(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    assert demo.post("/api/events", json={"run_id": run_id}).status_code == 401
    assert signed_post(demo, run_id, key_run_id="run-0000000000000000").status_code == 401  # key of another run
    assert signed_post(demo, run_id, ts=time.time() - 3600).status_code == 401  # replay outside the window
    assert signed_post(demo, "run-ffffffffffffffff").status_code == 404  # valid signature, unknown run
    assert signed_post(demo, run_id).json() == {"status": "accepted"}
    assert signed_post(demo, run_id).json() == {"status": "duplicate"}
    assert signed_post(demo, run_id, event_id="evt-00000002", event_type="model_validation_completed"
                       ).status_code == 200
    assert signed_post(demo, run_id, event_type="Bad Type").status_code == 422

    events = demo.get(f"/api/demo/{run_id}/events", headers=auth()).json()["items"]
    assert [e["event_type"] for e in events] == ["model_validation_started", "model_validation_completed"]
    assert events[0]["payload"] == {"seconds": 1.5}
    assert demo.get(f"/api/demo/{run_id}/events", params={"after_id": events[0]["id"]},
                    headers=auth()).json()["items"][0]["event_id"] == "evt-00000002"
    # Events prove the notebook started, but do not mark the run complete.
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["status"] == "running"


def test_events_survive_run_completion(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    signed_post(demo, run_id)
    conn = store.connect()
    store.finish(conn, run_id, "complete")
    conn.close()
    assert len(demo.get(f"/api/demo/{run_id}/events", headers=auth()).json()["items"]) == 1
    assert signed_post(demo, run_id, event_id="evt-late-0001").json() == {"status": "accepted"}


def test_runs_are_visible_only_to_their_owner(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth("auth0|bob")).status_code == 404
    assert demo.get(f"/api/demo/{run_id}/events", headers=auth("auth0|bob")).status_code == 404
    assert demo.get("/api/demo/runs", headers=auth("auth0|bob")).json()["items"] == []
    assert [r["run_id"] for r in demo.get("/api/demo/runs", headers=auth()).json()["items"]] == [run_id]


def test_notebook_staging_injects_run_config_without_the_master_secret(demo, tmp_path, monkeypatch):
    src = tmp_path / "nb"
    src.mkdir()
    (src / "kernel-metadata.json").write_text(json.dumps({"id": "u/nb", "code_file": "nb.ipynb"}))
    (src / "nb.ipynb").write_text(json.dumps({"cells": [{"cell_type": "code", "source": ["print(1)"]}],
                                              "nbformat": 4, "nbformat_minor": 5}))
    monkeypatch.setattr(config, "KAGGLE_NOTEBOOK_DIR", src)
    out = tmp_path / "staged"
    kaggle_launcher.stage_notebook("run-0123456789abcdef", out)
    raw = (out / "nb.ipynb").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    first = "".join(json.loads(raw)["cells"][0]["source"])
    assert "run-0123456789abcdef" in first and demo_auth.run_event_key("run-0123456789abcdef") in first
    assert "test-secret" not in first
    compile(first, "prelude", "exec")
    assert json.loads(raw)["cells"][1]["source"] == ["print(1)"]
    assert (src / "nb.ipynb").read_text() != raw.decode()  # source notebook untouched


def test_submitting_run_without_progress_times_out(demo, monkeypatch):
    conn = store.connect()
    run = store.create_run(conn, "auth0|alice")
    monkeypatch.setattr(config, "DEMO_SUBMIT_TIMEOUT_SECONDS", -1)
    assert demo_service.sync_run(conn, run)["status"] == "failed"
    conn.close()


def test_status_reports_phase_and_does_not_treat_complete_as_pass(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    status = lambda: demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()  # noqa: E731
    assert status()["phase"] is None and status()["result"] == "pending"
    signed_post(demo, run_id, event_id="evt-00000001", event_type="model_ready")
    assert status()["phase"] == "model_ready" and status()["result"] == "pending"
    conn = store.connect()
    store.finish(conn, run_id, "complete")
    conn.close()
    assert status()["result"] == "incomplete"  # Kaggle COMPLETE without the final passing event
    signed_post(demo, run_id, event_id="evt-00000002", event_type="model_validation_completed",
                payload={"status": "passed"})
    assert status()["phase"] == "validated" and status()["result"] == "passed"


def test_failed_validation_event_marks_result_failed(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    signed_post(demo, run_id, event_type="modelcheck_completed", payload={"status": "failed"})
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["result"] == "failed"


def test_repository_notebook_stages_and_reports_all_phases(demo):
    staged = kaggle_launcher.render_prelude("run-0123456789abcdef")
    notebook = json.loads((config.ROOT / "kaggle-notebook" / "notebookb061a06458.ipynb").read_text("utf-8"))
    code = "\n".join("".join(c["source"]) for c in notebook["cells"])
    compile(staged + "\n" + code, "notebook", "exec")
    for event_type in demo_service.PHASES:
        assert f'"{event_type}"' in code
    assert "webhook.site" not in code
