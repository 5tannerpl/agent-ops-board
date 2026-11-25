"""Stored legacy history; reconciliation is a caller decision, never a lookup side effect."""
def owned_history(conn, owner, run_id):
    row = conn.execute("SELECT * FROM demo_runs WHERE owner_sub=? AND run_id=?", (owner, run_id)).fetchone()
    return dict(row) if row else None
