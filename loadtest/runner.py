"""Synthetic load-test scenarios: producer, run lifecycle, finalisation and result files."""
import csv
import json
import os
import platform
import random
import shutil
import subprocess
import threading
import time

import config
from adapters import synthetic
from intake.service import submit_unit
from loadtest.metrics import run_metrics, unit_timings
from storage import db
from workers.model_client import make_client

SCENARIOS = {
    "multi_channel_350": "Units arrive evenly across the arrival window; the deadline applies to completion.",
    "burst_350": "All units are injected at time zero; measures drain time.",
}
FAULT_KINDS = ("LLM_TIMEOUT_ONCE", "INVALID_JSON_ONCE", "HANDOFF_FAIL_ONCE", "WORKER_CRASH_ONCE",
               "RESOURCE_EXHAUSTED_ONCE")
DEMO_FAULTS = {"LLM_TIMEOUT_ONCE": 3, "INVALID_JSON_ONCE": 2, "HANDOFF_FAIL_ONCE": 2, "WORKER_CRASH_ONCE": 1}


class Conflict(Exception):
    """A load test is already running."""


def build_plan(target: int, seed: int, faults: dict[str, int]) -> list[tuple[str, str | None]]:
    rng = random.Random(seed)
    pattern = ["email"] * 8 + ["chat"] * 7 + ["voice"] * 5  # 40/35/25 mix
    channels = [pattern[i % 20] for i in range(target)]
    rng.shuffle(channels)
    fault_at: dict[int, str] = {}
    slots = rng.sample(range(target), min(sum(faults.values()), target))
    names = [k for k, n in faults.items() for _ in range(n)]
    for idx, name in zip(slots, names):
        fault_at[idx] = name
    return [(channels[i], fault_at.get(i)) for i in range(target)]


def create_run(conn, scenario: str, target_units: int, arrival_window: float, deadline: float,
               worker_count: int, faults: dict[str, int] | None = None, seed: int = 1) -> str:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    if scenario == "burst_350":
        arrival_window = 0
    faults = {k: int(v) for k, v in (faults or {}).items() if k in FAULT_KINDS and int(v) > 0}
    with db.tx(conn):
        if conn.execute("SELECT 1 FROM load_test_runs WHERE state='RUNNING'").fetchone():
            raise Conflict("a load test is already running")
        run_id = db.next_id(conn, "run", "RUN", 3)
        conn.execute(
            "INSERT INTO load_test_runs(test_run_id, scenario, target_count, target_seconds, "
            "arrival_window_seconds, worker_count, started_at, state, config_json) VALUES(?,?,?,?,?,?,?, 'RUNNING', ?)",
            (run_id, scenario, target_units, deadline, arrival_window, worker_count, db.now(),
             json.dumps({"seed": seed, "faults": faults})))
        db.set_setting(conn, "desired_workers", str(worker_count))
    return run_id


def start_producer(run_id: str) -> threading.Thread:
    thread = threading.Thread(target=produce, args=(run_id,), name=f"producer-{run_id}", daemon=True)
    thread.start()
    return thread


def produce(run_id: str) -> None:
    conn = db.connect()
    run = dict(conn.execute("SELECT * FROM load_test_runs WHERE test_run_id=?", (run_id,)).fetchone())
    cfg = json.loads(run["config_json"])
    plan = build_plan(run["target_count"], cfg["seed"], cfg["faults"])
    window, started = run["arrival_window_seconds"], run["started_at"]
    produced = 0
    for i, (channel, fault) in enumerate(plan):
        if conn.execute("SELECT stop_requested FROM load_test_runs WHERE test_run_id=?", (run_id,)).fetchone()[0]:
            break
        due = started + (i * window / len(plan) if window else 0)
        while (delay := due - time.time()) > 0:
            time.sleep(min(delay, 0.5))
        submit_unit(conn, channel, synthetic.generate(channel, cfg["seed"] * 100003 + i),
                    test_run_id=run_id, fault=fault)
        produced += 1
        conn.execute("UPDATE load_test_runs SET produced_count=? WHERE test_run_id=?", (produced, run_id))
    conn.execute("UPDATE load_test_runs SET producer_done=1 WHERE test_run_id=?", (run_id,))
    while finalize_if_done(conn, run_id) == "RUNNING":
        time.sleep(1)
    conn.close()


def request_stop(conn, run_id: str) -> None:
    """Stops producing; queued units are still processed, never discarded."""
    conn.execute("UPDATE load_test_runs SET stop_requested=1 WHERE test_run_id=? AND state='RUNNING'", (run_id,))


def hardware_info() -> dict:
    info = {"platform": platform.platform(), "python": platform.python_version(), "cpu_count": os.cpu_count()}
    try:
        with open("/proc/cpuinfo") as f:
            info["cpu_model"] = next((l.split(":", 1)[1].strip() for l in f if l.startswith("model name")), None)
        with open("/proc/meminfo") as f:
            info["memory_gb"] = round(int(f.readline().split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    if shutil.which("nvidia-smi"):
        try:
            info["gpu"] = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    return info


def finalize_if_done(conn, run_id: str) -> str:
    """Idempotently close a run once production ended and every unit reached a terminal state."""
    row = conn.execute("SELECT * FROM load_test_runs WHERE test_run_id=?", (run_id,)).fetchone()
    if row["state"] != "RUNNING" or not row["producer_done"]:
        return row["state"]
    open_units = conn.execute(
        "SELECT COUNT(*) FROM intake_units WHERE test_run_id=? AND status NOT IN ('COMPLETED','FAILED','MANUAL_REVIEW')",
        (run_id,)).fetchone()[0]
    if open_units:
        return "RUNNING"
    with db.tx(conn):
        run = conn.execute("SELECT * FROM load_test_runs WHERE test_run_id=?", (run_id,)).fetchone()
        if run["state"] != "RUNNING":
            return run["state"]
        run = dict(run)
        last = conn.execute("SELECT MAX(completed_at) FROM intake_units WHERE test_run_id=?", (run_id,)).fetchone()[0]
        run["finished_at"] = last or db.now()
        run["state"] = "FINISHED"
        metrics = run_metrics(conn, run)
        duration = run["finished_at"] - run["started_at"]
        completed = metrics["counts"]["completed"]
        stopped = run["stop_requested"] and run["produced_count"] < run["target_count"]
        all_completed = completed == run["target_count"]
        within = duration <= run["target_seconds"]
        client = make_client()
        summary = {
            "metrics": metrics, "duration_seconds": round(duration, 2),
            "acceptance": {"all_completed": all_completed, "within_deadline": within,
                           "deadline_seconds": run["target_seconds"], "passed": all_completed and within},
            "model": {"backend": client.backend, "name": client.model, "simulated": client.simulated,
                      "server": client.server_version()},
            "hardware": hardware_info(), "workers": run["worker_count"],
        }
        state = "STOPPED" if stopped else "FINISHED"
        conn.execute("UPDATE load_test_runs SET state=?, finished_at=?, summary_json=? WHERE test_run_id=?",
                     (state, run["finished_at"], json.dumps(summary), run_id))
    write_results(conn, run_id, summary)
    return state


def write_results(conn, run_id: str, summary: dict) -> None:
    out = config.RESULTS_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    cols = ["unit_id", "channel", "status", "attempt", "created_at", "started_at", "completed_at", "ingest_ms",
            "queue_wait_s", "inference_s", "persistence_s", "handoff_s", "e2e_s"]
    with open(out / "performance.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(unit_timings(conn, run_id))
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
