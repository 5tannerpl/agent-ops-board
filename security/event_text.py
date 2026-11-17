"""Dependency-free text rules shared by event producers (sanitise) and the ingest schemas (reject)."""
import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_URL = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S*")
_SECRET = re.compile(
    r"(?i)(?:sha256=[0-9a-f]+"
    r"|\bbearer\s+\S+"
    r"|\b(?:secret|password|passwd|token|api[_-]?key|authorization|signature)\b\s*[=:]\s*\S+"
    r"|\b(?:sk|ghp|gho|ghs|github_pat|xox[abp])[-_][A-Za-z0-9_-]{16,}"
    r"|\b[0-9a-f]{32,}\b"
    r"|\b[A-Za-z0-9_-]{40,}\b)")

URL_PLACEHOLDER, SECRET_PLACEHOLDER = "[url]", "[redacted]"


def is_unsafe(text: str) -> bool:
    """True for multi-line/raw-log text, URLs and anything that looks like a credential."""
    return bool(_CONTROL.search(text) or _URL.search(text) or _SECRET.search(text))


def safe_text(value, limit: int = 200) -> str:
    """One short line with URLs and credential-like tokens replaced; always accepted by the event schemas."""
    text = _URL.sub(URL_PLACEHOLDER, str(value))
    text = _SECRET.sub(SECRET_PLACEHOLDER, text)
    text = " ".join(_CONTROL.sub(" ", text).split())
    return text[:limit]
