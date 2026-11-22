"""Production startup guards."""
from pathlib import Path
from urllib.parse import urlsplit

import config


def _missing_file(path: Path) -> bool:
    try:
        return not path.is_file()
    except OSError:
        return True


def _is_staging_events_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "event-ingress"
        and port == 8011
        and parsed.path == "/api/events"
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def validate_production_config() -> None:
    if config.AOB_ENV not in ("production", "staging"):
        return
    missing = []
    if not config.AUTH0_DOMAIN:
        missing.append("AOB_AUTH0_DOMAIN/AUTH0_DOMAIN")
    if not config.AUTH0_AUDIENCE:
        missing.append("AOB_AUTH0_AUDIENCE/AUTH0_AUDIENCE")
    if _missing_file(config.ALLOWED_USERS_FILE):
        missing.append("AOB_ALLOWED_USERS_FILE")
    if not config.EVENT_SECRET_FILE.is_file() or not config.get_event_secret():
        missing.append("AOB_EVENT_SECRET_FILE")
    if config.AOB_ENV == "production":
        valid_events_url = (
            config.EVENTS_PUBLIC_URL.startswith("https://")
            and config.EVENTS_PUBLIC_URL.endswith("/api/events")
        )
        url_hint = "AOB_EVENTS_URL=https://.../api/events"
    else:
        valid_events_url = _is_staging_events_url(config.EVENTS_PUBLIC_URL)
        url_hint = "AOB_EVENTS_URL=http://event-ingress:8011/api/events"
    if not valid_events_url:
        missing.append(url_hint)
    if missing:
        raise RuntimeError("Missing required production settings: " + ", ".join(missing))
