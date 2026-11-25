"""Private operator control process: python -m crosscloud.worker (after API migration)."""
import os
import signal
from threading import Event

import config
from security.production_config import validate_production_config
from .dispatch import step


def main():
    if not (config.CROSS_CLOUD_ENABLED and config.REMOTE_RUNS_ENABLED):
        return
    import fcntl  # OCI Linux; an advisory lock on the shared, local SQLite volume.
    if os.getenv("AOB_KAGGLE_RUN_DEPLOYMENT_ID"):
        validate_production_config()
    elif config.AOB_ENV in ("production", "staging") and not config.ALLOWED_USERS_FILE.is_file():
        raise SystemExit("Missing AOB_ALLOWED_USERS_FILE")
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with open(str(config.DEMO_DB_PATH) + ".dispatcher.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Dispatcher already running for this database") from None
        while not stop.is_set():
            if not step():
                stop.wait(1)


if __name__ == "__main__":
    main()
