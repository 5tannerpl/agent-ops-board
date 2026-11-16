import http.server
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

import config
from scaling import gpu_manager, policy
from scaling.autoscaler import Autoscaler
from scaling.events import EventPublisher
from scaling.gpu_manager import LlamaInstance
from scaling.inference_router import InferenceRouter
from scaling.scenario import Environment, run_scenario
from scaling.settings import for_mode, tuned

HIGH = for_mode("high_concurrency")
LIGHT = for_mode("light")
FAKE = Path(__file__).with_name("fake_llama_server.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_llama_binary(tmp_path: Path) -> Path:
    script = tmp_path / "llama-server"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    script.chmod(0o755)
    return script


# ---- policy -----------------------------------------------------------------------------------

def test_worker_target_climbs_one_tier_at_a_time_and_scales_in_only_when_idle():
    assert policy.worker_target(HIGH, 2, pending=98, running=2, idle_for_s=0) == 4
    assert policy.worker_target(HIGH, 4, pending=60, running=4, idle_for_s=0) == 8
    assert policy.worker_target(HIGH, 4, pending=12, running=4, idle_for_s=0) is None  # below the 8-worker tier
    assert policy.worker_target(HIGH, 8, pending=0, running=3, idle_for_s=60) is None  # never kill busy workers
    assert policy.worker_target(HIGH, 8, pending=0, running=0, idle_for_s=14) is None
    assert policy.worker_target(HIGH, 8, pending=0, running=0, idle_for_s=15) == 2
    assert policy.worker_target(LIGHT, 2, pending=500, running=2, idle_for_s=0) is None


def test_gpu1_starts_only_when_every_condition_holds():
    ok = dict(gpu1_state="STOPPED", gpu1_available=True, gpu0_ready=True, actual_workers=4, backlog_seconds=11,
              predicted_drain_s=80, cooling_down=False, attempts=0)
    assert policy.gpu1_start_reason(HIGH, **ok)
    for key, bad in [("gpu1_available", False), ("gpu0_ready", False), ("actual_workers", 2),
                     ("backlog_seconds", 5), ("predicted_drain_s", 10), ("cooling_down", True),
                     ("attempts", 2), ("gpu1_state", "READY")]:
        assert policy.gpu1_start_reason(HIGH, **{**ok, key: bad}) is None, key
    assert policy.gpu1_start_reason(LIGHT, **ok) is None
    assert policy.gpu1_start_reason(HIGH, **{**ok, "predicted_drain_s": policy.INF})


def test_verdict_needs_measured_evidence_for_both_gpus():
    good = dict(total=100, completed=100, failed=0, final_workers=2, peak_workers=8, confirmed_worker_levels=[2, 4, 8],
                final_gpu_instances=1, gpu0_requests=60, gpu1_requests=40, gpu1_ready=True,
                gpu1_stopped_cleanly=True, telemetry_failed_events=0)
    assert policy.verdict(HIGH, good) == ("passed", [])
    assert policy.verdict(HIGH, {**good, "gpu1_requests": 0})[1] == ["GPU1_NO_INFERENCE_REQUESTS"]
    assert policy.verdict(HIGH, {**good, "gpu1_ready": False, "gpu1_error": "GPU1_START_FAILED"}) == (
        "degraded", ["GPU1_START_FAILED"])
    assert "GPU_UNAVAILABLE" in policy.verdict(HIGH, {**good, "gpu1_ready": False, "gpu1_unavailable": True})[1]
    assert policy.verdict(HIGH, {**good, "confirmed_worker_levels": [2, 8]})[1] == ["WORKERS_4_NOT_CONFIRMED"]
    assert policy.verdict(HIGH, {**good, "final_gpu_instances": 2, "gpu1_stopped_cleanly": False})[0] == "degraded"
    assert policy.verdict(HIGH, {**good, "completed": 0})[0] == "failed"
    assert policy.verdict(HIGH, {**good, "timed_out": True})[0] == "failed"
    light = dict(total=10, completed=10, failed=0, final_workers=2, peak_workers=2)
    assert policy.verdict(LIGHT, light) == ("passed", [])
    assert policy.verdict(LIGHT, {**light, "gpu1_ready": True})[0] == "degraded"


# ---- router -----------------------------------------------------------------------------------

def stub_backend(name: str, delay: float = 0.0):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(delay)
            data = json.dumps({"served_by": name}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def post(url: str):
    import httpx
    return httpx.post(url + "/v1/chat/completions", json={"x": 1}, timeout=10)


def test_router_balances_counts_and_never_sends_to_a_draining_backend():
    s0, u0 = stub_backend("gpu0", 0.2)
    s1, u1 = stub_backend("gpu1", 0.2)
    router = InferenceRouter(port=0)
    router.add_backend("gpu0", u0)
    router.start()
    try:
        assert post(router.url).json()["served_by"] == "gpu0"
        router.add_backend("gpu1", u1)
        threads = [threading.Thread(target=post, args=(router.url,)) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        stats = router.stats()
        assert stats["gpu1"]["completed"] >= 3 and stats["gpu0"]["completed"] >= 3
        router.drain("gpu1")
        assert {post(router.url).json()["served_by"] for _ in range(4)} == {"gpu0"}
        assert router.inflight("gpu1") == 0
        router.remove("gpu1")
        assert router.stats()["gpu1"]["completed"] == stats["gpu1"]["completed"]  # evidence survives removal
        router.drain("gpu0")
        assert post(router.url).status_code == 503
    finally:
        router.stop()
        s0.shutdown()
        s1.shutdown()


# ---- event publisher ---------------------------------------------------------------------------

def test_publisher_coalesces_snapshots_but_never_drops_lifecycle_events():
    gate, seen = threading.Event(), []

    def emit(event_type, data):
        gate.wait(5)
        seen.append((event_type, data))
        return event_type != "bad"

    pub = EventPublisher(emit, max_pending_snapshots=2)
    pub.publish("scaling_demo_started", {})
    time.sleep(0.1)  # first event is now blocked inside emit
    for i in range(6):
        pub.publish("scaling_metrics_snapshot", {"i": i})
    pub.publish("gpu_instance_ready", {})
    pub.publish("bad", {})
    gate.set()
    assert pub.close(10)
    types = [t for t, _ in seen]
    assert types[0] == "scaling_demo_started" and "gpu_instance_ready" in types
    assert [d["i"] for t, d in seen if t == "scaling_metrics_snapshot"] == [4, 5]
    assert pub.failed == 1 and pub.dropped == 4


# ---- autoscaler (deterministic clock, stub queue) -----------------------------------------------

class StubQueue:
    def __init__(self):
        self.c = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "total": 100, "producer_done": False,
                  "state": "RUNNING", "oldest_pending_seconds": None}
        self.workers = 2

    def counts(self):
        return dict(self.c)

    def actual_workers(self):
        return self.workers


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def build_autoscaler(settings, gpu1, queue, events, available=False):
    router = InferenceRouter(port=0)
    gpu0 = LlamaInstance("gpu0", 0, 1, "x", "m", "alias", "/dev/null")
    gpu0.adopt()
    clock = Clock()
    auto = Autoscaler(settings, queue, router, gpu0, gpu1, lambda t, d: events.append((t, d)),
                      lambda n: setattr(queue, "workers", n), gpu1_available=available, stats_fn=lambda: {},
                      clock=clock, wall=clock)
    return auto, clock


def test_autoscaler_steps_workers_2_4_8_2_from_queue_readings():
    events, queue = [], StubQueue()
    gpu1 = LlamaInstance("gpu1", 1, 1, "x", "m", "alias", "/dev/null")
    auto, clock = build_autoscaler(HIGH, gpu1, queue, events)
    queue.c.update(pending=98, running=2)
    for _ in range(5):
        auto.tick()
        clock.t += 2
    queue.c.update(pending=0, running=0, completed=100, producer_done=True)
    for _ in range(14):
        auto.tick()
        clock.t += 2
    requested = [d for t, d in events if t == "worker_scaling_requested"]
    assert [(d["from"], d["to"]) for d in requested] == [(2, 4), (4, 8), (8, 2)]
    done = [d for t, d in events if t == "worker_scaling_completed"]
    assert [(d["to"], d["actual_workers"], d["status"]) for d in done] == [(4, 4, "confirmed"), (8, 8, "confirmed"),
                                                                           (2, 2, "confirmed")]
    assert auto.peak_workers == 8 and auto.confirmed_levels == {2, 4, 8}
    snaps = [d for t, d in events if t == "scaling_metrics_snapshot"]
    assert snaps and {"actual_workers", "desired_workers", "requests_by_gpu", "gpu1"} <= set(snaps[0])
    assert any(s["scaling_state"] == "SCALE_OUT" for s in snaps) or any(s["actual_workers"] == 8 for s in snaps)


def test_failed_gpu1_start_is_reported_and_never_marked_ready(tmp_path):
    events, queue = [], StubQueue()
    gpu1 = LlamaInstance("gpu1", 1, free_port(), fake_llama_binary(tmp_path), tmp_path / "m.gguf", "alias",
                         tmp_path / "gpu1.log", env={**os.environ, "FAKE_LLAMA_FAIL_DEVICE": "CUDA1"},
                         startup_timeout=10)
    auto, clock = build_autoscaler(tuned(HIGH, sustained_backlog_seconds=2, predicted_drain_gt_seconds=1), gpu1,
                                   queue, events, available=True)
    queue.c.update(pending=98, running=2, producer_done=True)
    for _ in range(6):
        auto.tick()
        clock.t += 2
        if auto._gpu_thread:
            auto._gpu_thread.join(20)
    types = [t for t, _ in events]
    assert "gpu_instance_starting" in types and "gpu_instance_failed" in types
    assert "gpu_instance_ready" not in types and not auto.gpu1_ever_ready
    assert auto.gpu1_error == "GPU1_START_FAILED" and gpu1.state == gpu_manager.FAILED


# ---- end to end: real processes, fake llama-server ----------------------------------------------

def test_dual_gpu_scaling_end_to_end_with_fake_llama(tmp_path, monkeypatch):
    for name in ("RUNTIME_DIR", "RAW_DIR", "ATTACHMENT_DIR", "RESULTS_DIR", "DB_PATH", "MODEL_BACKEND", "MODEL_URL",
                 "MODEL_NAME", "MODEL_TIMEOUT"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setenv("FAKE_LLAMA_LATENCY", "2.0")
    monkeypatch.setattr(os, "environ", os.environ.copy())  # configure_runtime() rewrites AOB_* variables
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x" * 1024)
    gpus = {0: {"memory_used_mb": 4000, "memory_total_mb": 15000, "utilization_pct": 50},
            1: {"memory_used_mb": 300, "memory_total_mb": 15000, "utilization_pct": 0}}
    env = Environment(repo_dir=config.ROOT, runtime_dir=tmp_path / "rt", llama_server=fake_llama_binary(tmp_path),
                      model_path=model, gpu0_port=free_port(), gpu1_port=free_port(), router_port=free_port(),
                      require_gpu=False, stats_fn=lambda: gpus)
    settings = tuned(for_mode("high_concurrency", 50), run_timeout_seconds=150, poll_seconds=0.5, snapshot_seconds=1,
                     scale_out_pending_4=5, scale_out_pending_8=15, idle_before_scale_in_seconds=3,
                     worker_cooldown_seconds=1, sustained_backlog_seconds=2, predicted_drain_gt_seconds=3,
                     gpu_startup_timeout_seconds=30, gpu_drain_timeout_seconds=30)
    events = []
    lock = threading.Lock()

    def emit(event_type, data):
        with lock:
            events.append((event_type, data))
        return True

    summary = run_scenario(settings, env, emit)

    types = [t for t, _ in events]
    assert summary["status"] == "passed", summary
    assert types[0] == "gpu_instance_ready" and "scaling_demo_started" in types and types[-1] == "scaling_demo_completed"
    moves = [(d["from"], d["to"]) for t, d in events if t == "worker_scaling_requested"]
    assert moves == [(2, 4), (4, 8), (8, 2)]
    order = [t for t, d in events if t.startswith("gpu_instance") and d.get("gpu_id") == 1]
    for earlier, later in [("gpu_instance_starting", "gpu_instance_ready"), ("gpu_instance_ready", "gpu_instance_draining"),
                           ("gpu_instance_draining", "gpu_instance_stopped")]:
        assert order.index(earlier) < order.index(later)
    assert summary["gpu0_inference_requests"] > 0 and summary["gpu1_inference_requests"] > 0
    assert summary["completed"] == 50 and summary["failed"] == 0
    assert summary["peak_workers"] == 8 and summary["final_workers"] == 2
    assert summary["peak_gpu_instances"] == 2 and summary["final_gpu_instances"] == 1
    assert summary["gpu1_stopped_cleanly"] is True and summary["gpu1_load_seconds"] is not None
    assert summary["scale_in_seconds"] is not None and summary["p95_seconds"] is not None
    stopped = next(d for t, d in events if t == "gpu_instance_stopped")
    assert stopped["inflight_requests"] == 0 and stopped["port_released"] is True
    assert not gpu_manager.port_open(env.gpu1_port) and not gpu_manager.port_open(env.gpu0_port)


def test_scenario_reports_failure_when_no_gpu_is_visible(tmp_path, monkeypatch):
    events = []
    env = Environment(repo_dir=config.ROOT, runtime_dir=tmp_path, llama_server=tmp_path / "x",
                      model_path=tmp_path / "m", require_gpu=True, stats_fn=lambda: {})
    result = run_scenario(LIGHT, env, lambda t, d: events.append((t, d)) or True)
    assert result["status"] == "failed" and result["error_code"] == "GPU_UNAVAILABLE"
    assert [t for t, _ in events] == ["scaling_demo_failed"]
