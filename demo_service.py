"""Live-demo run lifecycle: start, background submission and Kaggle status synchronisation."""
import logging

import config
import demo_scaling
import kaggle_launcher
import demo_store as store
from storage import db

log = logging.getLogger("demo")


MODEL_NAME = "Qwen3-4B-Instruct-2507"
# Application-level phases, from notebook events (ordered). Kaggle scheduler state is reported separately.
PHASES = {"notebook_started": "initializing", "environment_ready": "initializing",
          "model_located": "loading_model", "model_ready": "model_ready",
          "model_validation_started": "validating", "worker_validation_completed": "validating",
          "modelcheck_completed": "validating", "model_validation_completed": "validated"}
_PHASE_ORDER = list(PHASES)


def summarize_run(conn, run: dict) -> dict:
    """Derive phase and business result from received events; never infers progress that was not reported.

    ``result`` is separate from the Kaggle status: a ``complete`` batch without the final passing event is
    ``incomplete``, not a pass.
    """
    events = store.list_events(conn, run["run_id"], 0, config.EVENT_MAX_PER_RUN)
    seen = {e["event_type"]: e["payload"] for e in events}
    known = [t for t in _PHASE_ORDER if t in seen]
    phase = PHASES[known[-1]] if known else None
    failed = any(seen.get(t, {}).get("status") == "failed"
                 for t in ("worker_validation_completed", "modelcheck_completed", "model_validation_completed"))
    if failed:
        result = "failed"
    elif seen.get("model_validation_completed", {}).get("status") == "passed":
        result = "passed"
    elif run["status"] in ("failed", "cancelled"):
        result = "failed"
    elif run["status"] == "complete":
        result = "incomplete"
    else:
        result = "pending"
    return {"phase": phase, "last_event_type": events[-1]["event_type"] if events else None,
            "last_event_at": events[-1]["received_at"] if events else None, "result": result,
            "model": MODEL_NAME, "event_count": len(events), "mode": run.get("mode"), "units": run.get("units"),
            "scaling_status": demo_scaling.status_of(run, events)}


def start_run(conn, owner_sub: str, mode: str = "light", units: int | None = None) -> dict:
    """Create a ``submitting`` run. Raises store.Conflict / store.RateLimited."""
    sync_active(conn, force=True)  # free the single slot if the previous batch already finished
    return store.create_run(conn, owner_sub, mode, units)


def submit_run(run_id: str) -> None:
    """Push to Kaggle (blocking; run in the background). A successful push only means ``queued``."""
    conn = store.connect()
    try:
        run = store.get_run(conn, run_id) or {}
        kaggle_launcher.submit(
            run_id, run.get("mode") or "light", run.get("units"), int(run.get("event_key_version") or 1)
        )
        store.advance(conn, run_id, "queued")
    except kaggle_launcher.LaunchError as exc:
        store.finish(conn, run_id, "failed", str(exc))
    except Exception:
        log.exception("unexpected error submitting %s", run_id)
        store.finish(conn, run_id, "failed", "unexpected launcher error")
    finally:
        conn.close()


def sync_active(conn, force: bool = False) -> None:
    for run in store.active_runs(conn):
        sync_run(conn, run, force)


def sync_run(conn, run: dict, force: bool = False) -> dict:
    """Reconcile one active run with timeouts and the Kaggle CLI status; returns the fresh row."""
    if run["status"] not in store.ACTIVE:
        return run
    now = db.now()
    age = now - run["created_at"]
    if age > config.DEMO_MAX_RUN_SECONDS:
        store.finish(conn, run["run_id"], "failed", "Timed out waiting for the Kaggle batch")
    elif run["status"] == "submitting":
        if age > config.DEMO_SUBMIT_TIMEOUT_SECONDS:
            store.finish(conn, run["run_id"], "failed", "Submission did not complete")
    elif force or not run["last_synced_at"] or now - run["last_synced_at"] >= config.DEMO_SYNC_INTERVAL_SECONDS:
        _apply_kaggle_status(conn, run, age)
    return store.get_run(conn, run["run_id"])


def _apply_kaggle_status(conn, run: dict, age: float) -> None:
    run_id = run["run_id"]
    try:
        kaggle = kaggle_launcher.kernel_status()
    except kaggle_launcher.LaunchError as exc:
        log.warning("status check for %s failed: %s", run_id, exc)
        store.record_sync(conn, run_id)
        return
    if kaggle in ("queued", "running"):
        store.record_sync(conn, run_id, kaggle, seen_active=True)
        store.advance(conn, run_id, kaggle)
        return
    # Right after a push Kaggle may still report the previous version's terminal state.
    if run["kaggle_seen_active"] or age > config.DEMO_STALE_STATUS_GRACE_SECONDS:
        store.record_sync(conn, run_id, kaggle)
        store.finish(conn, run_id, kaggle, "Kaggle reported an error" if kaggle == "failed" else None)
    else:
        store.record_sync(conn, run_id)
