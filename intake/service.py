"""Canonical intake: normalise a channel payload, persist raw content + metadata, enqueue a job."""
import time

import config
from storage import db, files

CHANNELS = ("email", "chat", "voice")


def _preview(text: str) -> str:
    return " ".join(text.split())[:120]


def _check_size(*texts: str) -> None:
    if sum(len(t or "") for t in texts) > config.MAX_TEXT_CHARS:
        raise ValueError("message too large")


def submit_unit(conn, channel: str, payload: dict, test_run_id: str | None = None,
                fault: str | None = None, received_at: float | None = None) -> str:
    """Store one intake unit and enqueue it. payload shape depends on channel:
    email {subject, body, sender?, attachments?[{filename, content}]}
    chat  {messages: [{speaker, text, timestamp?}]}
    voice {segments: [{speaker, text, start_s?}]}"""
    if channel not in CHANNELS:
        raise ValueError(f"unsupported channel {channel!r}")
    t0 = time.perf_counter()
    received_at = received_at or db.now()

    if channel == "email":
        _check_size(payload.get("subject", ""), payload.get("body", ""))
        if not str(payload.get("body", "")).strip():
            raise ValueError("email body is empty")
        items = [payload]
        preview = payload.get("subject") or payload["body"]
    else:
        key = "messages" if channel == "chat" else "segments"
        items = payload.get(key) or []
        if not items or not all(str(i.get("text", "")).strip() for i in items):
            raise ValueError(f"{channel} payload needs non-empty {key}")
        _check_size(*[i["text"] for i in items])
        first_customer = next((i for i in items if str(i.get("speaker", "")).lower() == "customer"), items[0])
        preview = first_customer["text"]

    with db.tx(conn):
        unit_id = db.next_id(conn, "unit", "INT")
        conv_id = db.next_id(conn, "conv", "CONV")
        msg_ids = [db.next_id(conn, "msg", "MSG") for _ in items]
        if channel == "email":
            raw = {"message_id": msg_ids[0], "sender": payload.get("sender", ""),
                   "subject": payload.get("subject", ""), "body": payload["body"],
                   "received_at": received_at}
            raw_id = msg_ids[0]
        elif channel == "chat":
            raw = {"conversation_id": conv_id, "messages": [
                {"message_id": m, "speaker": i.get("speaker", ""), "timestamp": i.get("timestamp"),
                 "text": i["text"]} for m, i in zip(msg_ids, items)]}
            raw_id = conv_id
        else:
            raw = {"transcript_id": conv_id, "segments": [
                {"message_id": m, "speaker": i.get("speaker", ""), "start_s": i.get("start_s"),
                 "text": i["text"]} for m, i in zip(msg_ids, items)]}
            raw_id = conv_id
        raw_path = files.write_raw(channel, raw_id, raw)
        for seq, m in enumerate(msg_ids):
            conn.execute(
                "INSERT INTO source_messages(message_id, channel, conversation_id, raw_path, received_at, seq) "
                "VALUES(?,?,?,?,?,?)", (m, channel, conv_id, raw_path, received_at, seq))
        for att in payload.get("attachments", []) if channel == "email" else []:
            att_id = db.next_id(conn, "att", "ATT")
            rel, size = files.write_attachment(att_id, att["filename"], att["content"].encode())
            conn.execute("INSERT INTO attachments VALUES(?,?,?,?,?)",
                         (att_id, unit_id, att["filename"], rel, size))
        now = db.now()
        conn.execute(
            "INSERT INTO intake_units(unit_id, channel, conversation_id, preview, status, test_run_id, fault, "
            "created_at) VALUES(?,?,?,?, 'PENDING', ?,?,?)",
            (unit_id, channel, conv_id, _preview(preview), test_run_id, fault, received_at))
        conn.execute(
            "INSERT INTO intake_jobs(unit_id, status, attempt, max_attempts, run_after, created_at, updated_at) "
            "VALUES(?, 'PENDING', 0, ?, ?, ?, ?)", (unit_id, config.MAX_ATTEMPTS, now, now, now))
        conn.execute("UPDATE intake_units SET ingest_ms=? WHERE unit_id=?",
                     ((time.perf_counter() - t0) * 1000, unit_id))
    return unit_id


def load_segments(conn, unit_id: str) -> list[dict]:
    """Rebuild ordered evidence-addressable segments from stored raw content."""
    unit = conn.execute("SELECT channel, conversation_id FROM intake_units WHERE unit_id=?", (unit_id,)).fetchone()
    if unit is None:
        raise files.CorruptInput("unknown unit")
    rows = conn.execute(
        "SELECT message_id, raw_path FROM source_messages WHERE conversation_id=? ORDER BY seq",
        (unit["conversation_id"],)).fetchall()
    if not rows:
        raise files.CorruptInput("no source messages")
    raw = files.read_raw(rows[0]["raw_path"])
    channel = unit["channel"]
    segments: list[dict] = []
    try:
        if channel == "email":
            mid = raw["message_id"]
            if raw.get("subject"):
                segments.append({"id": f"{mid}.subject", "kind": "subject", "speaker": "Customer",
                                 "timestamp": None, "text": raw["subject"]})
            segments.append({"id": mid, "kind": "body", "speaker": "Customer",
                             "timestamp": None, "text": raw["body"]})
        elif channel == "chat":
            for m in raw["messages"]:
                segments.append({"id": m["message_id"], "kind": "message", "speaker": m.get("speaker", ""),
                                 "timestamp": m.get("timestamp"), "text": m["text"]})
        else:
            for m in raw["segments"]:
                segments.append({"id": m["message_id"], "kind": "segment", "speaker": m.get("speaker", ""),
                                 "timestamp": m.get("start_s"), "text": m["text"]})
    except (KeyError, TypeError) as exc:
        raise files.CorruptInput("raw content has unexpected structure") from exc
    if not all(isinstance(s["text"], str) for s in segments):
        raise files.CorruptInput("non-text segment")
    return segments
