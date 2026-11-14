"""Handoff target selection and the mock downstream orchestrator (no real actions are taken)."""
import json

from storage import db

FACT_CHECK_LABELS = {"PAYMENT_VERIFICATION", "DEBT_DISPUTE", "ACCOUNT_BALANCE_REQUEST"}
COMPLIANCE_LABELS = {"HARDSHIP_NOTIFICATION", "CEASE_CONTACT_REQUEST", "COMPLAINT"}


class HandoffError(Exception):
    pass


def propose_targets(result: dict) -> list[str]:
    labels = {i["label"] for i in result["intents"]}
    unverified = any(not e["verified"] for i in result["intents"] for e in i["evidence"])
    targets = []
    if labels & FACT_CHECK_LABELS:
        targets.append("FACT_CHECK")
    if labels & COMPLIANCE_LABELS:
        targets.append("COMPLIANCE_CHECK")
    if "PAYMENT_ARRANGEMENT" in labels:
        targets.append("RESOLUTION_PLANNING")
    if not labels or "OTHER" in labels or unverified or not targets:
        targets.append("HUMAN_REVIEW")
    return targets


def deliver(conn, event_id: str, unit_id: str, payload: str) -> None:
    """Idempotent delivery: INSERT OR IGNORE on event_id means retries cause no duplicate effect."""
    json.loads(payload)
    conn.execute(
        "INSERT OR IGNORE INTO orchestrator_inbox(event_id, unit_id, payload, received_at) VALUES(?,?,?,?)",
        (event_id, unit_id, payload, db.now()))
