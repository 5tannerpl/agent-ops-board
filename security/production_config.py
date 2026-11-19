"""Production startup guards."""
from pathlib import Path

import config


def _missing_file(path: Path) -> bool:
    try:
        return not path.is_file()
    except OSError:
        return True


def validate_production_config() -> None:
    if config.AOB_ENV != "production":
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
    if not (config.EVENTS_PUBLIC_URL.startswith("https://") and config.EVENTS_PUBLIC_URL.endswith("/api/events")):
        missing.append("AOB_EVENTS_URL=https://.../api/events")
    if missing:
        raise RuntimeError("Missing required production settings: " + ", ".join(missing))
