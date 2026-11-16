import pytest
from streamlit.testing.v1 import AppTest

import api_client
import live_demo

STATE = {
    "run_id": "run-0123456789abcdef", "status": "running", "mode": "high_concurrency", "units": 100,
    "scaling_status": "running", "live": True, "scaling_state": "SCALE_OUT", "telemetry_delayed": False,
    "seconds_since_last_event": 2.0, "failure": None, "summary": None,
    "snapshot": {"pending": 60, "running": 8, "completed": 30, "failed": 0, "total": 100, "actual_workers": 8,
                 "desired_workers": 8, "gpu_instances_ready": 2,
                 "gpu0": {"state": "READY", "port": 8080, "memory_used_mb": 4800, "utilization_pct": 85},
                 "gpu1": {"state": "READY", "port": 8081, "memory_used_mb": None, "utilization_pct": None},
                 "requests_by_gpu": {"gpu0": 20, "gpu1": 0}, "inflight_by_gpu": {"gpu0": 4, "gpu1": 0}},
    "timeline": [{"id": 1, "received_at": 1.0, "event_type": "worker_scaling_requested",
                  "label": "Workers 2 → 4 requested (queue depth 98 >= 10)", "data": {}}]}
STATUS = {"status": "running", "kaggle_status": "running", "mode": "high_concurrency", "event_count": 9,
          "created_at": 1.0, "last_event_at": 2.0, "phase": "validated", "result": "passed", "model": "Qwen",
          "error": None}
POINTS = [{"t": 0, "pending": 98, "running": 2, "actual_workers": 2, "desired_workers": 4, "gpu_instances_ready": 1,
           "gpu0_requests": 0, "gpu1_requests": None, "gpu0_memory_mb": 4800},
          {"t": 3, "pending": 90, "running": 4, "actual_workers": 4, "desired_workers": 4, "gpu_instances_ready": 1,
           "gpu0_requests": 5, "gpu1_requests": None, "gpu0_memory_mb": 4800}]


def app():
    import live_demo as panel
    panel._run_panel("t", "run-0123456789abcdef", False)


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setattr(api_client, "demo_status", lambda *_: STATUS)
    monkeypatch.setattr(api_client, "demo_state", lambda *_: STATE)
    monkeypatch.setattr(api_client, "demo_metrics", lambda *_: POINTS)
    monkeypatch.setattr(api_client, "demo_events", lambda *_a, **_k: [
        {"received_at": 1.0, "event_type": "model_validation_completed", "payload": {"status": "passed"}}])


def test_panel_shows_workers_gpus_and_timeline_without_inventing_values(fake_api):
    at = AppTest.from_function(app, default_timeout=20).run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Actual / desired workers"] == "8 / 8" and metrics["GPU instances ready"] == "2 / 2"
    assert "N/A" in [m.value for m in at.metric]  # GPU 1 memory was not reported
    assert any("READY · 0 requests" in c.value for c in at.caption)  # ready is not the same as serving


def test_panel_flags_delayed_telemetry_and_failed_verdict(fake_api, monkeypatch):
    failed = {**STATE, "telemetry_delayed": True, "seconds_since_last_event": 61, "scaling_status": "failed",
              "live": False, "failure": {"error_code": "GPU_UNAVAILABLE", "phase": "gpu0", "message": "none"}}
    monkeypatch.setattr(api_client, "demo_state", lambda *_: failed)
    at = AppTest.from_function(app, default_timeout=20).run()
    assert any("Telemetry delayed" in w.value for w in at.warning)
    assert any("GPU_UNAVAILABLE" in e.value for e in at.error)


def test_legacy_run_shows_only_the_validation_section(fake_api, monkeypatch):
    monkeypatch.setattr(api_client, "demo_state", lambda *_: {**STATE, "scaling_status": None, "timeline": [],
                                                              "snapshot": None, "live": False})
    at = AppTest.from_function(app, default_timeout=20).run()
    assert not at.exception and not at.metric


def test_chart_frames_keep_missing_readings_as_gaps():
    frames = live_demo.chart_frames(POINTS)
    assert list(frames["Workers"].columns) == ["Actual workers", "Desired workers"]
    assert list(frames["Inference requests served"].columns) == ["GPU 0"]  # GPU 1 never reported anything
    assert "GPU utilization %" not in frames
