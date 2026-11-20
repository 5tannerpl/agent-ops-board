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
                           "modelcheck_completed", "model_validation_completed"], start=1):
        emit(demo, run_id, n, t, **({"status": "running" if t == "model_validation_started" else "passed"}
                                    if t != "notebook_started" else {}))
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
    # A passing model validation is not a scaling pass.
    emit(demo, run_id, 6, "scaling_demo_completed", status="degraded", reasons=["GPU1_NO_INFERENCE_REQUESTS"])
    summary = demo.get(f"/api/demo/{run_id}/summary", headers=auth()).json()
    assert summary["scaling_status"] == "degraded" and summary["summary"]["reasons"]
    # A finished Kaggle batch without scaling completion is incomplete.
    conn = store.connect()
    store.finish(conn, run_id, "complete")
    conn.close()
    run2 = start(demo, mode="high_concurrency")
    emit(demo, run2, 1, "model_validation_completed", status="passed")
    conn = store.connect()
    store.finish(conn, run2, "complete")
    conn.close()
    assert demo.get(f"/api/demo/{run2}/state", headers=auth()).json()["scaling_status"] == "incomplete"


def test_failure_event_marks_scaling_failed_and_telemetry_delay_is_flagged(demo):
    run_id = start(demo, mode="high_concurrency")
    emit(demo, run_id, 1, "scaling_demo_started", mode="high_concurrency")
    conn = store.connect()
    conn.execute("UPDATE demo_events SET received_at=received_at-120 WHERE run_id=?", (run_id,))
    conn.close()
    state = demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()
    assert state["telemetry_delayed"] is True and state["seconds_since_last_event"] >= 100
    emit(demo, run_id, 2, "scaling_demo_failed", phase="gpu0", error_code="GPU_UNAVAILABLE")
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


def test_traced_staging_pins_both_checkouts_and_rolls_back(demo, tmp_path, monkeypatch):
    from tests.test_event_contract import NOTEBOOKS
    import os

    cells = json.loads(NOTEBOOKS[1].read_text())["cells"]
    run_id = "run-0123456789abcdef"
    monkeypatch.setattr(kaggle_launcher, "_cli_checked", fake_pull(cells))
    monkeypatch.setenv("AOB_OTEL_ENABLED", "true")
    monkeypatch.setenv("AOB_OTEL_COMMIT", "main")
    with pytest.raises(kaggle_launcher.LaunchError, match="full Git commit"):
        kaggle_launcher.stage_notebook(run_id, tmp_path)
    monkeypatch.setenv("AOB_OTEL_COMMIT", "a" * 40)
    kaggle_launcher.stage_notebook(run_id, tmp_path)
    staged = json.loads((tmp_path / "nb.ipynb").read_text())["cells"]
    sources = ["".join(c["source"]) for c in staged]
    monkeypatch.setattr(os, "environ", os.environ.copy())
    ns = {}
    exec(sources[0], ns)
    assert os.environ["AOB_RUN_ID"] == ns["AOB_RUN_ID"] == run_id
    assert os.environ["AOB_OTEL_ENABLED"] == "true"
    assert sum(s.startswith(kaggle_launcher._CHECKOUT_GUARD) for s in sources) == 1
    assert "_aob_repo = REPO_DIR" in sources[-1] and "git" not in sources[-1]
    monkeypatch.setattr(kaggle_launcher, "_cli_checked", fake_pull(staged))
    monkeypatch.setenv("AOB_OTEL_ENABLED", "false")
    kaggle_launcher.stage_notebook(run_id, tmp_path)
    assert "aob_trace_checkout" not in (tmp_path / "nb.ipynb").read_text()


def test_events_can_be_filtered_by_type_with_a_stable_cursor(demo):
    run_id = start(demo)
    emit(demo, run_id, 1, "notebook_started")
    emit(demo, run_id, 2, "scaling_metrics_snapshot", **snapshot())
    emit(demo, run_id, 3, "model_validation_completed", status="passed")
    items = demo.get(f"/api/demo/{run_id}/validation", headers=auth()).json()["items"]
    assert [i["event_type"] for i in items] == ["notebook_started", "model_validation_completed"]


def test_bootstrap_reports_environment_problems_instead_of_skipping_silently(tmp_path, monkeypatch):
    from scaling import bootstrap
    monkeypatch.setattr(bootstrap, "CUDA_DIR", tmp_path / "none")
    monkeypatch.setattr(bootstrap, "_ensure_dependencies", lambda: None)
    sent = []
    ns = {"aob_emit": lambda t, d: sent.append((t, d)) or True, "AOB_DEMO_MODE": "light"}
    with pytest.raises(FileNotFoundError):
        bootstrap.run(ns)
    assert sent[0][0] == "scaling_demo_failed" and sent[0][1]["phase"] == "environment"


def test_scaling_views_sanitize_malicious_historical_payloads(demo):
    run_id = start(demo, mode="high_concurrency")
    conn = store.connect()
    now = 1000.0
    raw_events = [
        ("evt-raw-0001", "scaling_demo_started", {
            "mode": "high_concurrency", "planned_units": 150, "secret": "do-not-leak"
        }),
        ("evt-raw-0002", "scaling_controller_error", {
            "error": "RuntimeError: token=super-secret-value", "message": "traceback..."
        }),
        ("evt-raw-0003", "scaling_metrics_snapshot", {
            "pending": 80, "running": 4, "completed": 10, "failed": 0, "actual_workers": 4, "desired_workers": 8,
            "gpu_instances_ready": 1, "requests_by_gpu": {"gpu0": 16, "gpu1": 0, "gpu9": 999},
            "inflight_by_gpu": {"gpu0": 3, "gpu1": 1, "gpuX": 99}, "units_by_gpu": {"gpu0": 8, "gpu1": 2},
            "gpu0": {"state": "READY", "memory_used_mb": 4200, "utilization_pct": 77, "port": 8080},
            "gpu1": {"state": "STOPPED", "memory_used_mb": -1, "utilization_pct": 200},
        }),
        ("evt-raw-0004", "scaling_demo_completed", {
            "status": "degraded", "reasons": ["GPU1_NO_INFERENCE_REQUESTS", "LEAK=token", "WORKERS_8_NOT_CONFIRMED"],
            "completed": 100, "total": 100, "throughput_per_min": 12.5
        }),
        ("evt-raw-0005", "scaling_demo_failed", {
            "error_code": "ValueError", "phase": "router;drop table", "message": "internal stack with token"
        }),
    ]
    for event_id, event_type, payload in raw_events:
        conn.execute(
            "INSERT INTO demo_events(run_id, event_id, event_type, payload, event_ts, received_at) VALUES(?,?,?,?,?,?)",
            (run_id, event_id, event_type, json.dumps(payload), now, now),
        )
        now += 1
    conn.commit()
    conn.close()

    state = demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()
    assert state["plan"] == {"mode": "high_concurrency", "planned_units": 150}
    assert "token=super-secret-value" not in " ".join(e["label"] for e in state["timeline"])
    controller = next(e for e in state["timeline"] if e["event_type"] == "scaling_controller_error")
    assert controller["label"] == "Controller error: SCALING_FAILED"
    assert state["summary"]["reasons"] == ["GPU1_NO_INFERENCE_REQUESTS", "WORKERS_8_NOT_CONFIRMED"]
    assert state["failure"] == {
        "error_code": "SCALING_FAILED",
        "phase": "summary",
        "message": "Scaling execution failed.",
    }
    assert state["snapshot"]["requests_by_gpu"] == {"gpu0": 16, "gpu1": 0}
    assert "gpu9" not in json.dumps(state["snapshot"])

    points = demo.get(f"/api/demo/{run_id}/metrics", headers=auth()).json()["points"]
    assert len(points) == 1
    assert points[0]["gpu0_requests"] == 16 and points[0]["gpu1_requests"] == 0
    assert points[0]["gpu1_utilization_pct"] is None  # invalid 200 was dropped

    summary = demo.get(f"/api/demo/{run_id}/summary", headers=auth()).json()
    assert summary["summary"]["reasons"] == ["GPU1_NO_INFERENCE_REQUESTS", "WORKERS_8_NOT_CONFIRMED"]
    assert summary["failure"]["error_code"] == "SCALING_FAILED"


def test_scaling_views_handle_non_dict_historical_payloads(demo):
    run_id = start(demo, mode="high_concurrency")
    conn = store.connect()
    conn.execute(
        "INSERT INTO demo_events(run_id, event_id, event_type, payload, event_ts, received_at) VALUES(?,?,?,?,?,?)",
        (run_id, "evt-raw-nondict-1", "scaling_demo_started", json.dumps(["bad", "payload"]), 1.0, 1.0),
    )
    conn.execute(
        "INSERT INTO demo_events(run_id, event_id, event_type, payload, event_ts, received_at) VALUES(?,?,?,?,?,?)",
        (run_id, "evt-raw-nondict-2", "scaling_metrics_snapshot", json.dumps("oops"), 2.0, 2.0),
    )
    conn.commit()
    conn.close()

    state = demo.get(f"/api/demo/{run_id}/state", headers=auth()).json()
    assert state["plan"] is None and state["snapshot"] is None
    points = demo.get(f"/api/demo/{run_id}/metrics", headers=auth()).json()["points"]
    assert len(points) == 1 and points[0]["pending"] is None
