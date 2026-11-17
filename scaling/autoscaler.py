"""The single controller that turns real queue/worker/GPU readings into scaling actions and telemetry.

Only one Autoscaler may write ``desired_workers``. Every number it reports is read back from the SQLite queue,
the workers table (live PID + fresh heartbeat), the router counters or the instances' own /health.
"""
import os
import threading
import time
from collections import deque

import config
from scaling import gpu_manager, policy
from scaling.settings import ScalingSettings
from storage import db

ACTIVE_UNIT = ("PENDING", "WAITING_RETRY")


class QueueReader:
    def __init__(self, conn, test_run_id: str):
        self.conn, self.run_id = conn, test_run_id

    def counts(self) -> dict:
        by = {r[0]: r[1] for r in self.conn.execute(
            "SELECT status, COUNT(*) FROM intake_units WHERE test_run_id=? GROUP BY status", (self.run_id,))}
        oldest = self.conn.execute(
            "SELECT MIN(created_at) FROM intake_units WHERE test_run_id=? AND status IN ('PENDING','WAITING_RETRY')",
            (self.run_id,)).fetchone()[0]
        run = self.conn.execute("SELECT producer_done, target_count, state FROM load_test_runs WHERE test_run_id=?",
                                (self.run_id,)).fetchone()
        return {
            "pending": sum(by.get(s, 0) for s in ACTIVE_UNIT), "running": by.get("RUNNING", 0),
            "completed": by.get("COMPLETED", 0), "failed": by.get("FAILED", 0) + by.get("MANUAL_REVIEW", 0),
            "total": run["target_count"] if run else sum(by.values()),
            "producer_done": bool(run and run["producer_done"]), "state": run["state"] if run else None,
            "oldest_pending_seconds": round(db.now() - oldest, 1) if oldest else None}

    def completed_unit_ids(self) -> set[str]:
        if not self.run_id:
            return set()

        rows = self.conn.execute(
            """
            SELECT unit_id
            FROM intake_units
            WHERE test_run_id = ?
            AND status = 'COMPLETED'
            """,
            (self.run_id,)
        )

        return {row[0] for row in rows}


    def actual_workers(self) -> int:
        """Workers that are really alive: not OFFLINE, fresh heartbeat and an existing OS process."""
        horizon = db.now() - config.UNHEALTHY_AFTER
        rows = self.conn.execute("SELECT pid FROM workers WHERE status!='OFFLINE' AND last_heartbeat>?", (horizon,))
        return sum(1 for r in rows if r["pid"] and _pid_alive(r["pid"]))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Autoscaler:
    def __init__(self, settings: ScalingSettings, queue: QueueReader, router, gpu0, gpu1, publish, set_desired,
                 gpu1_available: bool = True, model_size_mb: int = 0, stats_fn=gpu_manager.gpu_stats,
                 clock=time.monotonic, wall=time.time):
        self.s, self.queue, self.router, self.gpu0, self.gpu1 = settings, queue, router, gpu0, gpu1
        self.publish, self.set_desired = publish, set_desired
        self.gpu1_available, self.model_size_mb = gpu1_available, model_size_mb
        self.stats_fn, self.clock, self.wall = stats_fn, clock, wall

        self.desired = settings.min_workers
        self._request: dict | None = None
        self._last_request_at = -1e9
        self._idle_since = self._backlog_since = None
        self._samples: deque = deque()
        self._last_snapshot = -1e9
        self._gpu_thread: threading.Thread | None = None
        self._gpu_cooldown_until = 0.0
        self._baseline_gpu1_mb: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # evidence
        self.actual = settings.min_workers
        self.peak_workers = 0
        self.peak_gpu_instances = 0
        self.confirmed_levels: set[int] = set()
        self.gpu1_attempts = 0
        self.gpu1_error: str | None = None
        self.gpu1_ever_ready = False
        self.gpu1_stopped_cleanly: bool | None = None
        self.gpu1_load_seconds: float | None = None
        self.t_scale_out_start = self.t_scale_out_done = self.t_scale_in_start = self.t_scale_in_done = None
        self.t_gpu_stopped: float | None = None
        self.last_counts: dict = {}

    # ---- thread control --------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="autoscaler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
        if self._gpu_thread:
            self._gpu_thread.join(timeout=self.s.gpu_drain_timeout_seconds + 60)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # a bad reading must not kill the controller
                self.publish("scaling_controller_error", {"error": type(exc).__name__, "message": str(exc)[:200]})
            self._stop.wait(self.s.poll_seconds)

    def gpu_busy(self) -> bool:
        return bool(self._gpu_thread and self._gpu_thread.is_alive())

    # ---- one control iteration -------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        q = self.queue.counts()
        self.last_counts = q
        self.actual = self.queue.actual_workers()
        self.peak_workers = max(self.peak_workers, self.actual)
        self.peak_gpu_instances = max(self.peak_gpu_instances, self._ready_instances())
        self._track(q, now)
        self._settle_request(now)
        self._decide_workers(q, now)
        self._decide_gpu(q, now)
        if now - self._last_snapshot >= self.s.snapshot_seconds:
            self._last_snapshot = now
            self.publish("scaling_metrics_snapshot", self.snapshot(q))

    def _track(self, q: dict, now: float) -> None:
        idle = q["producer_done"] and q["pending"] == 0 and q["running"] == 0
        self._idle_since = (self._idle_since or now) if idle else None
        self._backlog_since = (self._backlog_since or now) if q["pending"] >= self.s.scale_out_pending_4 else None
        self._samples.append((now, q["completed"]))
        while self._samples and now - self._samples[0][0] > 30:
            self._samples.popleft()

    def _rate(self) -> float | None:
        if len(self._samples) < 2:
            return None
        (t0, c0), (t1, c1) = self._samples[0], self._samples[-1]
        return (c1 - c0) / (t1 - t0) if t1 - t0 >= 5 else None

    def _idle_for(self, now: float) -> float:
        return now - self._idle_since if self._idle_since else 0.0

    # ---- workers ---------------------------------------------------------
    def _decide_workers(self, q: dict, now: float) -> None:
        if now - self._last_request_at < self.s.worker_cooldown_seconds:
            return
        target = policy.worker_target(self.s, self.desired, q["pending"], q["running"], self._idle_for(now))
        if target is None or target == self.desired:
            return
        if self._request and target > self.desired:
            return  # finish confirming the previous scale-out step first
        if self._request:
            self._close_request("superseded")
        self._issue(target, q, now)

    def _issue(self, target: int, q: dict, now: float) -> None:
        if target < self.desired:
            reason = f"queue idle for {self._idle_for(now):.0f}s"
        else:
            threshold = self.s.scale_out_pending_8 if target == self.s.max_workers else self.s.scale_out_pending_4
            reason = f"queue depth {q['pending']} >= {threshold}"
        if target > self.desired and self.t_scale_out_start is None:
            self.t_scale_out_start = now
        if target < self.desired:
            self.t_scale_in_start = now
        self._request = {"from": self.desired, "to": target, "at": now}
        self._last_request_at = now
        self.publish("worker_scaling_requested", {
            "from": self.desired, "to": target, "reason": reason, "queue_depth": q["pending"],
            "cooldown_until": round(self.wall() + self.s.worker_cooldown_seconds, 3)})
        self.desired = target
        self.set_desired(target)

    def _settle_request(self, now: float) -> None:
        req = self._request
        if not req:
            return
        if self.actual == req["to"]:
            self._close_request("confirmed")
        elif now - req["at"] > self.s.worker_scaling_timeout_seconds:
            self._close_request("timeout")

    def _close_request(self, status: str) -> None:
        req, self._request = self._request, None
        now = self.clock()
        if status == "confirmed":
            self.confirmed_levels.add(req["to"])
            if req["to"] == self.s.max_workers and self.t_scale_out_done is None and req["to"] > req["from"]:
                self.t_scale_out_done = now
            if req["to"] == self.s.min_workers and req["to"] < req["from"]:
                self.t_scale_in_done = now
        self.publish("worker_scaling_completed", {
            "status": status, "from": req["from"], "to": req["to"], "desired_workers": self.desired,
            "actual_workers": self.actual, "duration_seconds": round(now - req["at"], 2)})

    # ---- GPU 1 -----------------------------------------------------------
    def _decide_gpu(self, q: dict, now: float) -> None:
        if self.gpu_busy() or self.s.mode != "high_concurrency":
            return
        state = "STOPPED" if self.gpu1.state in (gpu_manager.STOPPED, gpu_manager.FAILED) else self.gpu1.state
        reason = policy.gpu1_start_reason(
            self.s, gpu1_state=state, gpu1_available=self.gpu1_available, gpu0_ready=self.gpu0.state == gpu_manager.READY,
            actual_workers=self.actual, backlog_seconds=now - self._backlog_since if self._backlog_since else 0.0,
            predicted_drain_s=policy.predicted_drain_seconds(q["pending"], self._rate()),
            cooling_down=now < self._gpu_cooldown_until, attempts=self.gpu1_attempts)
        if reason:
            self._gpu_thread = threading.Thread(target=self._start_gpu1, args=(reason,), name="gpu1-start", daemon=True)
            self._gpu_thread.start()
        elif policy.gpu1_should_stop(self.s, gpu1_state=self.gpu1.state, pending=q["pending"], running=q["running"],
                                     idle_for_s=self._idle_for(now)):
            self._gpu_thread = threading.Thread(target=self._stop_gpu1, args=("queue idle",), name="gpu1-stop",
                                                daemon=True)
            self._gpu_thread.start()

    def _gpu1_memory_mb(self) -> int | None:
        return (self.stats_fn().get(1) or {}).get("memory_used_mb")

    def _fail_gpu1(self, code: str, message: str | None = None) -> None:
        self.gpu1_error = code
        self._gpu_cooldown_until = self.clock() + self.s.gpu_start_cooldown_seconds
        self.publish("gpu_instance_failed", {"gpu_id": 1, "port": self.gpu1.port, "error_code": code,
                                             "message": (message or "")[:200], "attempt": self.gpu1_attempts,
                                             "status": "failed"})

    def _start_gpu1(self, reason: str) -> None:
        self.gpu1_attempts += 1
        self.publish("gpu_instance_starting", {"gpu_id": 1, "port": self.gpu1.port, "reason": reason})
        stats = self.stats_fn()
        gpu = stats.get(1)
        if gpu and gpu.get("memory_total_mb") is not None and gpu.get("memory_used_mb") is not None:
            free = gpu["memory_total_mb"] - gpu["memory_used_mb"]
            if free < self.model_size_mb + self.s.min_gpu_free_headroom_mb:
                return self._fail_gpu1("GPU1_INSUFFICIENT_MEMORY", f"{free} MB free on GPU 1")
        self._baseline_gpu1_mb = (gpu or {}).get("memory_used_mb")
        if not self.gpu1.start():
            return self._fail_gpu1("GPU1_START_FAILED", f"{self.gpu1.error} | {self.gpu1.error_log_tail()}")
        bound = gpu_manager.gpu_index_of_pid(self.gpu1.pid) if self.gpu1.pid else None
        if bound is not None and bound != 1:
            self.gpu1.stop()
            return self._fail_gpu1("GPU1_WRONG_DEVICE", f"process holds GPU {bound}")
        used = self._gpu1_memory_mb()
        grew = used - self._baseline_gpu1_mb if used is not None and self._baseline_gpu1_mb is not None else None
        self.gpu1_ever_ready, self.gpu1_load_seconds, self.gpu1_error = True, self.gpu1.load_seconds, None
        self.publish("gpu_instance_ready", {
            "gpu_id": 1, "port": self.gpu1.port, "model": self.gpu1.model_alias, "status": "ready",
            "load_duration_seconds": self.gpu1.load_seconds, "memory_used_mb": used, "memory_delta_mb": grew,
            "device_binding": self.gpu1.pinned_via, "device_binding_verified": None if bound is None else True})
        self.router.add_backend("gpu1", self.gpu1.url)
        self.publish("inference_route_changed", {"active_backends": self.router.active_backends(),
                                                 "reason": "GPU 1 ready"})

    def _stop_gpu1(self, reason: str) -> None:
        self.router.drain("gpu1")
        self.gpu1.mark_draining()
        self.publish("inference_route_changed", {"active_backends": self.router.active_backends(),
                                                 "reason": "GPU 1 draining"})
        self.publish("gpu_instance_draining", {"gpu_id": 1, "inflight_requests": self.router.inflight("gpu1")})
        deadline = self.clock() + self.s.gpu_drain_timeout_seconds
        while self.router.inflight("gpu1") and self.clock() < deadline:
            time.sleep(0.5)
        inflight = self.router.inflight("gpu1")
        exit_code = self.gpu1.stop()
        self.router.remove("gpu1")
        released = self._wait_memory_released()
        port_released = not gpu_manager.port_open(self.gpu1.port)
        self.gpu1_stopped_cleanly = bool(inflight == 0 and not self.gpu1.alive() and port_released
                                         and released is not False)
        self.t_gpu_stopped = self.clock()
        self.publish("gpu_instance_stopped", {
            "gpu_id": 1, "port": self.gpu1.port, "exit_code": exit_code, "reason": reason,
            "inflight_requests": inflight, "port_released": port_released, "memory_released": released,
            "clean": self.gpu1_stopped_cleanly, "status": "stopped"})

    def _wait_memory_released(self) -> bool | None:
        """True once GPU 1 memory is back near its pre-start level; None when it cannot be measured."""
        if self._baseline_gpu1_mb is None:
            return None
        for _ in range(10):
            used = self._gpu1_memory_mb()
            if used is None:
                return None
            if used <= self._baseline_gpu1_mb + 300:
                return True
            time.sleep(1)
        return False

    # ---- telemetry -------------------------------------------------------
    def _ready_instances(self) -> int:
        return int(self.gpu0.state == gpu_manager.READY) + int(self.gpu1.state == gpu_manager.READY)

    def scaling_state(self) -> str:
        if self.s.mode != "high_concurrency":
            return "BASELINE"
        if self.gpu1.state == gpu_manager.DRAINING or (self.desired == self.s.min_workers
                                                       and self.actual > self.s.min_workers):
            return "DRAINING"
        if self.desired > self.s.min_workers or self.gpu1.state in (gpu_manager.STARTING, gpu_manager.READY):
            starting = self.gpu1.state == gpu_manager.STARTING or self._request is not None
            return "SCALE_OUT" if starting else "SCALED_OUT"
        return "BASELINE"

    
    def snapshot(self, q: dict | None = None) -> dict:
        q = q or self.last_counts or self.queue.counts()
        stats, rstats = self.stats_fn(), self.router.stats()

        units_by_gpu = self.router.unit_attribution(
            self.queue.completed_unit_ids()
        )

        def gpu(inst, idx):
            hw = stats.get(idx) or {}
            return {"state": inst.state, "port": inst.port, "memory_used_mb": hw.get("memory_used_mb"),
                    "utilization_pct": hw.get("utilization_pct")}

        def per_gpu(key):
            return {n: rstats.get(n, {}).get(key, 0) for n in ("gpu0", "gpu1")}

        drain = policy.predicted_drain_seconds(q["pending"], self._rate())
        return {
            "mode": self.s.mode, "scaling_state": self.scaling_state(), "pending": q["pending"],
            "running": q["running"], "completed": q["completed"], "failed": q["failed"], "total": q["total"],
            "actual_workers": self.actual, "desired_workers": self.desired,
            "gpu_instances_ready": self._ready_instances(), "gpu0": gpu(self.gpu0, 0), "gpu1": gpu(self.gpu1, 1),
            "requests_by_gpu": per_gpu("completed"), "inflight_by_gpu": per_gpu("inflight"),
            "units_by_gpu": {
                "gpu0": units_by_gpu["gpu0"],
                "gpu1": units_by_gpu["gpu1"],
            },
            "unattributed_units": units_by_gpu["unattributed"],
            "failed_by_gpu": per_gpu("failed"), "oldest_pending_seconds": q["oldest_pending_seconds"],
            "estimated_drain_seconds": None if drain == policy.INF else round(drain, 1)}
