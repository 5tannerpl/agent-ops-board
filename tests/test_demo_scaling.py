import json

import pytest

import config
import demo_scaling
import demo_store as store
import kaggle_launcher
from tests.test_demo_api import auth, demo, signed_post  # noqa: F401  (fixtures and helpers)


def start(demo, **body):
    resp = demo.post("/api/demo/start", headers=auth(), json=body or None)
    assert resp.status_code == 202, resp.text
    return resp.json()["run_id"]


def emit(demo, run_id, n, event_type, **payload):
    assert signed_post(demo, run_id, event_id=f"evt-{n:08d}", event_type=event_type, payload=payload).status_code == 200


def snapshot(**over):
    base = {"mode": "high_concurrency", "scaling_state": "SCALE_OUT", "pending": 80, "running": 4, "completed": 16,
            "failed": 0, "actual_workers": 4, "desired_workers": 8, "gpu_instances_ready": 1,
            "gpu0": {"state": "READY", "port": 8080, "memory_used_mb": 4800, "utilization_pct": 80},
            "gpu1": {"state": "STOPPED", "port": 8081, "memory_used_mb": None, "utilization_pct": None},
            "requests_by_gpu": {"gpu0": 16, "gpu1": 0}}
    return {**base, **over}


def test_start_accepts_mode_and_units_and_defaults_to_light(demo):
    light = start(demo)
    assert demo.get(f"/api/demo/{light}/state", headers=auth()).json()["mode"] == "light"
    conn = store.connect()
    store.finish(conn, light, "complete")
    conn.close()
    high = start(demo, mode="high_concurrency", units=150)
    status = demo.get(f"/api/demo/{high}/status", headers=auth()).json()
    assert (status["mode"], status["units"]) == ("high_concurrency", 150)
    conn = store.connect()
    store.finish(conn, high, "complete")
    conn.close()
    assert demo.post("/api/demo/start", headers=auth(), json={"mode": "turbo"}).status_code == 422
    assert demo.post("/api/demo/start", headers=auth(), json={"mode": "high_concurrency", "units": 7}).status_code == 422
    assert demo.post("/api/demo/start", headers=auth(), json={"mode": "light", "units": 50}).status_code == 422


def test_existing_five_events_and_old_runs_still_render(demo):
    run_id = start(demo)
    for n, t in enumerate(["notebook_started", "model_validation_started", "worker_validation_completed",
                           "modelcheck_completed", "model_validation_completed"]):
        emit(demo, run_id, n + 1, t, status="passed")
    state = demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()
    assert state["scaling_status"] is None and state["snapshot"] is None and state["timeline"] == []
    status = demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()
    assert status["result"] == "passed" and status["event_count"] == 5
    assert demo.get(f"/api/demo/{run_id}/metrics", headers=auth()).json() == {"points": []}
    assert demo.get(f"/api/demo/{run_id}/summary", headers=auth()).json()["summary"] is None


def test_state_metrics_and_summary_come_from_reported_events_only(demo):
    run_id = start(demo, mode="high_concurrency")
    emit(demo, run_id, 1, "model_validation_completed", status="passed")
    emit(demo, run_id, 2, "scaling_demo_started", mode="high_concurrency", planned_units=100)
    emit(demo, run_id, 3, "scaling_metrics_snapshot", **snapshot())
    emit(demo, run_id, 4, "worker_scaling_requested", **{"from": 2, "to": 4, "reason": "queue depth 98 >= 10"})
    emit(demo, run_id, 5, "scaling_metrics_snapshot", **snapshot(actual_workers=8, completed=40))
    state = demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()
    assert state["scaling_status"] == "running" and state["live"] is True
    assert state["snapshot"]["actual_workers"] == 8 and state["scaling_state"] == "SCALE_OUT"
    assert [e["event_type"] for e in state["timeline"]] == ["scaling_demo_started", "worker_scaling_requested"]
    assert "2 → 4" in state["timeline"][1]["label"]
    points = demo.get(f"/api/demo/{run_id}/metrics", headers=auth()).json()["points"]
    assert [p["actual_workers"] for p in points] == [4, 8] and points[0]["t"] == 0
    # A passing model validation or a finished Kaggle batch is not a scaling pass.
    conn = store.connect()
    store.finish(conn, run_id, "complete")
    conn.close()
    assert demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()["scaling_status"] == "incomplete"
    emit(demo, run_id, 6, "scaling_demo_completed", status="degraded", reasons=["GPU1_NO_INFERENCE_REQUESTS"])
    summary = demo.get(f"/api/demo/{run_id}/summary", headers=auth()).json()
    assert summary["scaling_status"] == "degraded" and summary["summary"]["reasons"]


def test_failure_event_marks_scaling_failed_and_telemetry_delay_is_flagged(demo):
    run_id = start(demo, mode="high_concurrency")
    emit(demo, run_id, 1, "scaling_demo_started", mode="high_concurrency")
    conn = store.connect()
    conn.execute("UPDATE demo_events SET received_at=received_at-120 WHERE run_id=?", (run_id,))
    conn.close()
    state = demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()
    assert state["telemetry_delayed"] is True and state["seconds_since_last_event"] >= 100
    emit(demo, run_id, 2, "scaling_demo_failed", status="failed", phase="gpu0", error_code="GPU_UNAVAILABLE")
    state = demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()
    assert state["scaling_status"] == "failed" and state["failure"]["error_code"] == "GPU_UNAVAILABLE"
    assert state["telemetry_delayed"] is False


def test_duplicate_scaling_events_do_not_duplicate_the_timeline(demo):
    run_id = start(demo, mode="high_concurrency")
    emit(demo, run_id, 1, "gpu_instance_ready", gpu_id=1)
    assert signed_post(demo, run_id, event_id="evt-00000001", event_type="gpu_instance_ready",
                       payload={"gpu_id": 1}).json()["status"] == "duplicate"
    assert len(demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()["timeline"]) == 1


def test_other_users_cannot_read_scaling_views(demo):
    run_id = start(demo)
    for view in ("state", "metrics", "summary"):
        assert demo.get(f"/api/demo/{run_id}/{view}", headers=auth("auth0|bob")).status_code == 404


def test_old_database_is_migrated_with_mode_columns(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE demo_runs (run_id TEXT PRIMARY KEY, owner_sub TEXT NOT NULL, status TEXT NOT NULL, "
                "kaggle_status TEXT, kaggle_seen_active INTEGER NOT NULL DEFAULT 0, error TEXT, created_at REAL "
                "NOT NULL, updated_at REAL NOT NULL, submitted_at REAL, last_synced_at REAL, finished_at REAL)")
    old.execute("INSERT INTO demo_runs(run_id, owner_sub, status, created_at, updated_at) "
                "VALUES('run-0123456789abcdef', 'u', 'complete', 1, 1)")
    old.commit()
    old.close()
    monkeypatch.setattr(config, "DEMO_DB_PATH", path)
    conn = store.connect()
    assert store.get_run(conn, "run-0123456789abcdef")["mode"] is None
    assert demo_scaling.status_of(store.get_run(conn, "run-0123456789abcdef"), []) is None
    conn.close()


# ---- launcher ------------------------------------------------------------------------------------

LEGACY_STEP6 = '''import time
sent = 0
failed = 0

def send_event(event_type, data):
    pass

send_event("model_validation_started", {})
send_event("worker_validation_completed", {})
send_event("modelcheck_completed", {})
send_event("model_validation_completed", {})
'''


def fake_pull(source_cells):
    def pull(args, timeout, operation):
        if operation != "pull":
            raise AssertionError(operation)
        target = __import__("pathlib").Path(args[args.index("-p") + 1])
        (target / "kernel-metadata.json").write_text(json.dumps({
            "id": "sillypanda5/notebookb061a06458", "code_file": "nb.ipynb",
            "dataset_sources": ["sillypanda5/agent-ops-qwen-model", "sillypanda5/agent-ops-llama-cuda-t4"]}))
        (target / "nb.ipynb").write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {},
                                                     "cells": source_cells}))
    return pull


def test_staged_notebook_gets_mode_prelude_and_exactly_one_scaling_cell(demo, tmp_path, monkeypatch):
    cells = [{"cell_type": "code", "metadata": {}, "outputs": [], "source": LEGACY_STEP6.splitlines(True)}]
    monkeypatch.setattr(kaggle_launcher, "_cli_checked", fake_pull(cells))
    out = tmp_path / "staged"
    kaggle_launcher.stage_notebook("run-0123456789abcdef", out, "high_concurrency", 150)
    staged = json.loads((out / "nb.ipynb").read_text())
    sources = ["".join(c["source"]) for c in staged["cells"]]
    assert "AOB_DEMO_MODE = 'high_concurrency'" in sources[0] and "AOB_DEMO_UNITS = 150" in sources[0]
    assert sources[-1].startswith(kaggle_launcher.SCALING_CELL_MARKER)
    compile(sources[0] + "\n" + sources[-1], "staged", "exec")
    assert "bootstrap.run(globals())" in sources[-1] and "test-secret" not in sources[-1]

    # Re-staging a previously staged notebook replaces the prelude and the scaling cell.
    monkeypatch.setattr(kaggle_launcher, "_cli_checked", fake_pull(staged["cells"]))
    again = tmp_path / "again"
    kaggle_launcher.stage_notebook("run-fedcba9876543210", again, "light")
    restaged = ["".join(c["source"]) for c in json.loads((again / "nb.ipynb").read_text())["cells"]]
    assert sum(s.startswith(kaggle_launcher.SCALING_CELL_MARKER) for s in restaged) == 1
    assert "run-0123456789abcdef" not in "".join(restaged) and "AOB_DEMO_UNITS = None" in restaged[0]


def test_prelude_rejects_bad_mode_and_units(demo):
    for kwargs in ({"mode": "x"}, {"mode": "high_concurrency", "units": "9; import os"}, {"units": True}):
        with pytest.raises(kaggle_launcher.LaunchError):
            kaggle_launcher.render_prelude("run-0123456789abcdef", **kwargs)


def test_events_can_be_filtered_by_type_with_a_stable_cursor(demo):
    run_id = start(demo)
    emit(demo, run_id, 1, "notebook_started")
    emit(demo, run_id, 2, "scaling_metrics_snapshot", **snapshot())
    emit(demo, run_id, 3, "model_validation_completed", status="passed")
    url = f"/api/demo/{run_id}/events"
    items = demo.get(url, params={"types": "notebook_started,model_validation_completed"}, headers=auth()).json()["items"]
    assert [i["event_type"] for i in items] == ["notebook_started", "model_validation_completed"]
    later = demo.get(url, params={"after_id": items[0]["id"]}, headers=auth()).json()["items"]
    assert [i["event_type"] for i in later] == ["scaling_metrics_snapshot", "model_validation_completed"]


def test_bootstrap_reports_environment_problems_instead_of_skipping_silently(tmp_path, monkeypatch):
    from scaling import bootstrap
    monkeypatch.setattr(bootstrap, "CUDA_DIR", tmp_path / "none")
    monkeypatch.setattr(bootstrap, "_ensure_dependencies", lambda: None)
    sent = []
    ns = {"aob_emit": lambda t, d: sent.append((t, d)) or True, "AOB_DEMO_MODE": "light"}
    with pytest.raises(FileNotFoundError):
        bootstrap.run(ns)
    assert sent[0][0] == "scaling_demo_failed" and sent[0][1]["phase"] == "environment"
