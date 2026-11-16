"""Live-demo run lifecycle: start, background submission and Kaggle status synchronisation."""
import logging

import config
import demo_launcher
import demo_store as store
from storage import db

log = logging.getLogger("demo")


def start_run(conn, owner_sub: str) -> dict:
    """Create a ``submitting`` run. Raises store.Conflict / store.RateLimited."""
    sync_active(conn, force=True)  # free the single slot if the previous batch already finished
    return store.create_run(conn, owner_sub)


def submit_run(run_id: str) -> None:
    """Push to Kaggle (blocking; run in the background). A successful push only means ``queued``."""
    conn = store.connect()
    try:
        demo_launcher.submit(run_id)
        store.advance(conn, run_id, "queued")
    except demo_launcher.LaunchError as exc:
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
        kaggle = demo_launcher.kernel_status()
    except demo_launcher.LaunchError as exc:
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
