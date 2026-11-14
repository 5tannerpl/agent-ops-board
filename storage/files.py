"""Raw content on the local file system, addressed by opaque ids (no traversal)."""
import json
import re
from pathlib import Path

import config

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class CorruptInput(Exception):
    pass


def safe_id(value: str) -> str:
    if not SAFE_ID.match(value or "") or ".." in value:
        raise ValueError(f"unsafe identifier: {value!r}")
    return value


def _resolve(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    if root.resolve() not in path.parents:
        raise CorruptInput("path escapes storage root")
    return path


def write_raw(channel: str, raw_id: str, payload: dict) -> str:
    """Persist a raw payload; returns the path relative to RAW_DIR."""
    rel = f"{safe_id(channel)}/{safe_id(raw_id)}.json"
    data = json.dumps(payload, ensure_ascii=False).encode()
    if len(data) > config.MAX_RAW_BYTES:
        raise ValueError("raw payload too large")
    path = _resolve(config.RAW_DIR, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def read_raw(rel: str) -> dict:
    path = _resolve(config.RAW_DIR, rel)
    try:
        if path.stat().st_size > config.MAX_RAW_BYTES:
            raise CorruptInput("raw file too large")
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CorruptInput(f"unreadable raw content: {type(exc).__name__}") from exc


def write_attachment(attachment_id: str, filename: str, content: bytes) -> tuple[str, int]:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)[:80] or "file"
    rel = f"{safe_id(attachment_id)}/{safe_name}"
    path = _resolve(config.ATTACHMENT_DIR, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content[: config.MAX_RAW_BYTES])
    return rel, len(content)


def read_attachment(rel: str) -> bytes:
    return _resolve(config.ATTACHMENT_DIR, rel).read_bytes()
