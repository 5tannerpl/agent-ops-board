"""Runs N worker processes sharing one model server; restarts crashed workers and follows the
desired worker count that the API stores in settings."""
import argparse
import multiprocessing as mp
import signal
import time

from storage import db
from workers import worker

RESTART_DELAY = 5.0


def _entry(worker_id: str) -> None:
    worker.main(worker_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--no-follow-api", action="store_true", help="ignore API-requested worker counts")
    args = parser.parse_args()

    conn = db.connect()
    db.init_schema(conn)
    db.set_setting(conn, "desired_workers", str(args.workers))
    ctx = mp.get_context("spawn")
    procs: dict[str, mp.Process] = {}
    died_at: dict[str, float] = {}
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"supervisor: {args.workers} worker(s); Ctrl-C to stop", flush=True)
    while running:
        desired = args.workers if args.no_follow_api else int(db.get_setting(conn, "desired_workers", str(args.workers)))
        wanted = [f"worker-{i:02d}" for i in range(1, desired + 1)]
        for wid in list(procs):
            if wid not in wanted:
                procs.pop(wid).terminate()
            elif not procs[wid].is_alive():
                print(f"supervisor: {wid} exited with {procs[wid].exitcode}", flush=True)
                procs.pop(wid)
                died_at[wid] = time.time()
        for wid in wanted:
            if wid not in procs and time.time() - died_at.get(wid, 0) >= RESTART_DELAY:
                procs[wid] = ctx.Process(target=_entry, args=(wid,), name=wid)
                procs[wid].start()
        time.sleep(1)
    for p in procs.values():
        p.terminate()
    for p in procs.values():
        p.join(timeout=30)


if __name__ == "__main__":
    main()
