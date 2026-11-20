"""End-to-end autoscaling scenario run inside one Kaggle batch (spec section 10, steps 3-12).

GPU 0 llama-server -> inference router -> SQLite queue + Supervisor (real worker processes) -> synthetic burst ->
Autoscaler (workers 2->4->8->2, GPU 1 on demand) -> evidence-based summary. Every event is derived from
measurements; failures are reported as failures.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import config
from loadtest import runner
from loadtest.metrics import run_metrics
from scaling import gpu_manager, policy
from scaling.autoscaler import Autoscaler, QueueReader
from scaling.events import EventPublisher
from scaling.gpu_manager import LlamaInstance
from scaling.inference_router import InferenceRouter
from scaling.settings import ScalingSettings
from security.event_text import safe_text
from storage import db

MODEL_NAME = "Qwen3-4B-Instruct-2507"
GPU0_ISOLATION_LIMIT_MB = 1000  # GPU 1 memory above this before we start means GPU 0's server spans both cards


class ScenarioError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, message or code


@dataclass
class Environment:
    repo_dir: Path
    runtime_dir: Path
    llama_server: Path
    model_path: Path
    model_alias: str = MODEL_NAME
    gpu0_port: int = 8080
    gpu1_port: int = 8081
    router_port: int = 8090
    require_gpu: bool = True
    stats_fn: object = gpu_manager.gpu_stats
    existing_server: subprocess.Popen | None = None  # a GPU 0 server another notebook cell started
    llama_env: dict = field(default_factory=lambda: dict(os.environ))


def configure_runtime(runtime_dir: Path, model_url: str, model_name: str) -> None:
    """Point this process and every child at one queue database and at the router."""
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    values = {"AOB_RUNTIME_DIR": str(runtime_dir), "AOB_DB_PATH": str(runtime_dir / "intake.db"),
              "AOB_MODEL_BACKEND": "llamacpp", "AOB_MODEL_URL": model_url, "AOB_MODEL_NAME": model_name,
              "AOB_MODEL_TIMEOUT": "120"}
    os.environ.update(values)
    config.RUNTIME_DIR, config.RAW_DIR = runtime_dir, runtime_dir / "raw"
    config.ATTACHMENT_DIR, config.RESULTS_DIR = runtime_dir / "attachments", runtime_dir / "results"
    config.DB_PATH = runtime_dir / "intake.db"
    config.MODEL_BACKEND, config.MODEL_URL, config.MODEL_NAME, config.MODEL_TIMEOUT = (
        "llamacpp", model_url, model_name, 120.0)


def _wait(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _ensure_gpu0(env: Environment, gpu0: LlamaInstance, stats: dict) -> None:
    if gpu0.healthy():
        used1 = (stats.get(1) or {}).get("memory_used_mb")
        if used1 is None or used1 <= GPU0_ISOLATION_LIMIT_MB:
            gpu0.adopt(env.existing_server.pid if env.existing_server else None)
            return
        # The existing server is spread over both GPUs, so GPU 1 could not host an independent instance.
        if env.existing_server is None or env.existing_server.poll() is not None:
            raise ScenarioError("GPU0_NOT_ISOLATED", "existing server uses GPU 1 and cannot be restarted")
        env.existing_server.terminate()
        if not _wait(lambda: not gpu_manager.port_open(gpu0.port), 30):
            raise ScenarioError("GPU0_NOT_ISOLATED", "could not free the GPU 0 port")
    if not gpu0.start():
        raise ScenarioError("GPU0_START_FAILED", gpu0.error)


def _terminate(proc: subprocess.Popen | None, timeout: float = 30.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)


def _port_free(port: int) -> bool:
    return not gpu_manager.port_open(port)


def run_scenario(settings: ScalingSettings, env: Environment, emit) -> dict:
    """Run the whole experiment; returns the summary (also published as scaling_demo_completed/failed)."""
    publisher = EventPublisher(emit)
    publish = publisher.publish
    phase = "setup"
    router = supervisor = autoscaler = gpu0 = gpu1 = None
    try:
        stats = env.stats_fn()
        if env.require_gpu and 0 not in stats:
            raise ScenarioError("GPU_UNAVAILABLE", "no NVIDIA GPU is visible to this batch")
        gpu1_available = 1 in stats or not env.require_gpu
        model_mb = round(env.model_path.stat().st_size / 1e6) if env.model_path.exists() else 0
        logs = env.runtime_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        make = lambda name, gid, port: LlamaInstance(  # noqa: E731
            name, gid, port, env.llama_server, env.model_path, env.model_alias, logs / f"llama-{name}.log",
            env=env.llama_env, startup_timeout=settings.gpu_startup_timeout_seconds)
        gpu0, gpu1 = make("gpu0", 0, env.gpu0_port), make("gpu1", 1, env.gpu1_port)

        phase = "gpu0"
        _ensure_gpu0(env, gpu0, stats)
        publish("gpu_instance_ready", {
            "gpu_id": 0, "port": gpu0.port, "model": env.model_alias, "status": "ready",
            "load_duration_seconds": gpu0.load_seconds, "adopted": not gpu0.owned,
            "memory_used_mb": (env.stats_fn().get(0) or {}).get("memory_used_mb"),
            "device_binding": gpu0.pinned_via})

        phase = "router"
        router = InferenceRouter(port=env.router_port)
        router.add_backend("gpu0", gpu0.url)
        router.start()
        if not gpu_manager.http_ok(router.url + "/health"):
            raise ScenarioError("ROUTER_NOT_READY", "inference router /health failed")
        publish("inference_route_changed", {"active_backends": router.active_backends(), "reason": "router started"})

        phase = "workers"
        configure_runtime(env.runtime_dir, router.url, env.model_alias)
        conn = db.connect()
        db.init_schema(conn)
        reader = QueueReader(conn, "")
        sup_log = open(logs / "supervisor.log", "w")
        supervisor = subprocess.Popen(
            [sys.executable, "-m", "workers.supervisor", "--workers", str(settings.min_workers)],
            cwd=str(env.repo_dir), env={**os.environ, "PYTHONPATH": str(env.repo_dir),
                                        "AOB_SCALING_MODE": settings.mode, "AOB_UNITS": str(settings.units)}, stdout=sup_log,
            stderr=subprocess.STDOUT)
        if not _wait(lambda: reader.actual_workers() >= settings.min_workers, 90):
            raise ScenarioError("WORKERS_NOT_STARTED", f"only {reader.actual_workers()} worker(s) became healthy")

        phase = "scenario"
        publish("scaling_demo_started", {
            "mode": settings.mode, "min_workers": settings.min_workers, "max_workers": settings.max_workers,
            "initial_gpu_instances": 1,
            "target_gpu_instances": 2 if settings.max_gpu_instances > 1 and gpu1_available else 1,
            "planned_units": settings.units, "arrival_window_seconds": settings.arrival_window_seconds,
            "gpu_count": len(stats) or None, "gpu1_available": gpu1_available, "model": env.model_alias})
        scenario = "burst_350" if settings.arrival_window_seconds == 0 else "multi_channel_350"
        run_id = runner.create_run(conn, scenario, settings.units, settings.arrival_window_seconds,
                                   settings.run_timeout_seconds, settings.min_workers)
        reader.run_id = run_id
        control_conn = db.connect()  # the controller thread gets its own connection
        autoscaler = Autoscaler(
            settings, QueueReader(control_conn, run_id), router, gpu0, gpu1, publish,
            lambda n: db.set_setting(control_conn, "desired_workers", str(n)), gpu1_available=gpu1_available,
            model_size_mb=model_mb, stats_fn=env.stats_fn)
        autoscaler.start()
        started = time.monotonic()
        runner.start_producer(run_id)

        phase = "load"
        finished = _wait(lambda: reader.counts()["state"] != "RUNNING", settings.run_timeout_seconds, 1.0)
        if not finished:
            runner.request_stop(conn, run_id)
            _wait(lambda: reader.counts()["state"] != "RUNNING", 60, 1.0)

        phase = "scale_in"
        if finished:
            _wait(lambda: _settled(settings, autoscaler, reader), settings.idle_before_scale_in_seconds
                  + settings.gpu_drain_timeout_seconds + 45, 1.0)
        autoscaler.stop()

        phase = "summary"
        summary = _summarize(settings, run_id, conn, reader, autoscaler, router, gpu0, gpu1, publisher, finished,
                             gpu1_available, time.monotonic() - started)
        publish("scaling_metrics_snapshot", autoscaler.snapshot())
        attributed_total = (
            summary["gpu0_units_completed"]
            + summary["gpu1_units_completed"]
            + summary["unattributed_units"]
        )

        if attributed_total != summary["completed"]:
            raise ScenarioError(
                "UNIT_ATTRIBUTION_MISMATCH",
                f"attributed={attributed_total}, completed={summary['completed']}"
            )
        publish("scaling_demo_completed", summary)
        return summary
    except Exception as exc:
        code = exc.code if isinstance(exc, ScenarioError) else type(exc).__name__
        message = safe_text(exc.message if isinstance(exc, ScenarioError) else exc, 300)
        failure = {"status": "failed", "phase": phase, "error_code": code, "message": message, "mode": settings.mode}
        publish("scaling_demo_failed", failure)
        return failure
    finally:
        if autoscaler:
            autoscaler.stop()
        _terminate(supervisor)
        if gpu1 and gpu1.owned:
            gpu1.stop()
        if router:
            router.stop()
        if gpu0 and gpu0.owned:
            gpu0.stop()
        publisher.close(120)


def _settled(s: ScalingSettings, a: Autoscaler, reader: QueueReader) -> bool:
    """Back at baseline: workers at minimum, no scaling request open, GPU 1 not serving or starting."""
    if a.gpu_busy() or a._request is not None or a.desired != s.min_workers:
        return False
    if a.actual != s.min_workers:
        return False
    return a.gpu1.state in (gpu_manager.STOPPED, gpu_manager.FAILED)


def _summarize(s, run_id, conn, reader, a, router, gpu0, gpu1, publisher, finished, gpu1_available,
               elapsed) -> dict:
    row = conn.execute("SELECT * FROM load_test_runs WHERE test_run_id=?", (run_id,)).fetchone()
    stored = json.loads(row["summary_json"]) if row["summary_json"] else None
    metrics = stored["metrics"] if stored else run_metrics(conn, dict(row))
    counts = metrics["counts"]
    duration = (stored or {}).get("duration_seconds") or round(elapsed, 2)
    completed = counts["completed"]
    final_workers = reader.actual_workers()
    gpu1_serving = gpu1.state == gpu_manager.READY and gpu1.healthy()
    final_gpu_instances = int(gpu0.healthy()) + int(gpu1_serving)
    rstats = router.stats()
    completed_unit_ids = {
        row[0]
        for row in conn.execute(
            """
            SELECT unit_id
            FROM intake_units
            WHERE test_run_id = ?
            AND status = 'COMPLETED'
            """,
            (run_id,)
        )
    }

    units_by_gpu = router.unit_attribution(completed_unit_ids)
    gpu1_requests = rstats.get("gpu1", {}).get("completed", 0)
    evidence = {
        "total": s.units, "completed": completed, "failed": counts["failed"] + counts["manual_review"],
        "timed_out": not finished, "final_workers": final_workers, "peak_workers": a.peak_workers,
        "confirmed_worker_levels": sorted(a.confirmed_levels), "final_gpu_instances": final_gpu_instances,
        "gpu0_requests": rstats.get("gpu0", {}).get("completed", 0), "gpu1_requests": gpu1_requests,
        "gpu1_ready": a.gpu1_ever_ready, "gpu1_unavailable": not gpu1_available, "gpu1_error": a.gpu1_error,
        "gpu1_stopped_cleanly": a.gpu1_stopped_cleanly, "telemetry_failed_events": publisher.failed}
    status, reasons = policy.verdict(s, evidence)

    def span(start, end):
        return round(end - start, 2) if start is not None and end is not None else None

    scale_in_end = max([t for t in (a.t_scale_in_done, a.t_gpu_stopped) if t is not None], default=None)
    return {
        "status": status, "reasons": reasons, "mode": s.mode, "total": s.units, "completed": completed,
        "failed": evidence["failed"], "retries": metrics["retry_attempts"], "duration_seconds": duration,
        "p50_seconds": metrics["latency_p50_s"], "p95_seconds": metrics["latency_p95_s"],
        "throughput_per_min": round(completed / duration * 60, 2) if duration and completed else None,
        "peak_workers": a.peak_workers, "peak_gpu_instances": a.peak_gpu_instances,
        "confirmed_worker_levels": evidence["confirmed_worker_levels"],
        "gpu0_inference_requests": evidence["gpu0_requests"], "gpu1_inference_requests": gpu1_requests,
        "gpu1_stopped_cleanly": a.gpu1_stopped_cleanly, "final_workers": final_workers,
        "final_gpu_instances": final_gpu_instances,
        "gpu0_units_completed": units_by_gpu["gpu0"],
        "gpu1_units_completed": units_by_gpu["gpu1"],
        "unattributed_units": units_by_gpu["unattributed"],
        "scale_out_seconds": span(a.t_scale_out_start, a.t_scale_out_done),
        "worker_scale_out_seconds": span(a.t_scale_out_start, a.t_scale_out_done),
        "gpu1_load_seconds": a.gpu1_load_seconds, "scale_in_seconds": span(a.t_scale_in_start, scale_in_end),
        "telemetry_failed_events": publisher.failed}
