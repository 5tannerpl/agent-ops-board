"""Operator-only evidence collection. No queue lease, database write or slot release."""
import argparse
from contextlib import closing
import json
import time

from . import dispatch, resolution, store
from .policy import ServiceError


def observe(local):
    dispatch.enabled()
    with closing(store.reader()) as conn:
        row = conn.execute("""
            SELECT b.*,d.state,c.snapshot FROM deployment_bindings b
            JOIN run_dispatch d USING(run_id) JOIN dispatch_context c USING(run_id)
            WHERE b.run_id=?""", (local,)).fetchone()
    if row is None or row["state"] != "uncertain":
        raise ServiceError(409, "NOT_UNCERTAIN")
    snapshot = json.loads(row["snapshot"])
    current = dispatch.authorized_deployment(row, snapshot)
    if current["provider"] != "azure":
        raise ServiceError(409, "NOT_SUPPORTED")
    # A fresh GET budget, not an extension of the stored execution deadline.
    client = resolution.resolve(current, snapshot["target"], time.time() + 10)
    result = client.get_status(row["remote_run_id"])
    return {"observed_at": time.time(), "run_id": local, "remote_run_id": row["remote_run_id"],
            "deployment_id": current["deployment_id"], "source_id": row["source_id"],
            "observation": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_run_id")
    try:
        print(json.dumps(observe(parser.parse_args().local_run_id)))
    except ServiceError as exc:
        parser.exit(1, f"{exc.code}\n")
