"""Headless benchmark: python -m loadtest --scenario burst_350 --workers 2 (workers must be running)."""
import argparse
import json
import sys
import time

from loadtest import runner
from storage import db


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", default="multi_channel_350", choices=list(runner.SCENARIOS))
    p.add_argument("--units", type=int, default=350)
    p.add_argument("--arrival-window", type=float, default=600)
    p.add_argument("--deadline", type=float, default=600)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--inject-faults", action="store_true", help="controlled failure demo")
    args = p.parse_args()
    conn = db.connect()
    db.init_schema(conn)
    run_id = runner.create_run(conn, args.scenario, args.units, args.arrival_window, args.deadline,
                               args.workers, runner.DEMO_FAULTS if args.inject_faults else None)
    print(f"started {run_id}", flush=True)
    runner.start_producer(run_id)
    while True:
        time.sleep(5)
        state = runner.finalize_if_done(conn, run_id)
        row = conn.execute("SELECT produced_count, target_count FROM load_test_runs WHERE test_run_id=?",
                           (run_id,)).fetchone()
        done = conn.execute("SELECT COUNT(*) FROM intake_units WHERE test_run_id=? AND status='COMPLETED'",
                            (run_id,)).fetchone()[0]
        print(f"{state} produced={row[0]}/{row[1]} completed={done}", flush=True)
        if state != "RUNNING":
            break
    summary = json.loads(conn.execute("SELECT summary_json FROM load_test_runs WHERE test_run_id=?",
                                      (run_id,)).fetchone()[0])
    print(json.dumps(summary["acceptance"] | {"duration_seconds": summary["duration_seconds"],
                                              "simulated_model": summary["model"]["simulated"]}, indent=2))
    return 0 if summary["acceptance"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
