"""Central configuration, overridable through AOB_* environment variables."""
import os
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("AOB_RUNTIME_DIR", ROOT / "runtime")).resolve()
RAW_DIR = RUNTIME_DIR / "raw"
ATTACHMENT_DIR = RUNTIME_DIR / "attachments"
RESULTS_DIR = RUNTIME_DIR / "results"
DB_PATH = Path(os.environ.get("AOB_DB_PATH", RUNTIME_DIR / "intake.db"))

MAX_RAW_BYTES = 1_000_000
MAX_TEXT_CHARS = 20_000

SCHEMA_VERSION = "1"

# Model serving. backend: ollama | llamacpp | mock (mock is NOT real inference).
MODEL_BACKEND = os.environ.get("AOB_MODEL_BACKEND", "ollama")
MODEL_NAME = os.environ.get("AOB_MODEL_NAME", "qwen2.5:3b")
MODEL_URL = os.environ.get(
    "AOB_MODEL_URL",
    "http://127.0.0.1:8080" if MODEL_BACKEND == "llamacpp" else "http://127.0.0.1:11434",
)
MODEL_TIMEOUT = float(os.environ.get("AOB_MODEL_TIMEOUT", "120"))
MOCK_LATENCY = float(os.environ.get("AOB_MOCK_LATENCY", "0"))

# Worker / lease behaviour.
LEASE_SECONDS = float(os.environ.get("AOB_LEASE_SECONDS", "20"))
HEARTBEAT_SECONDS = float(os.environ.get("AOB_HEARTBEAT_SECONDS", "2"))
POLL_SECONDS = float(os.environ.get("AOB_POLL_SECONDS", "0.25"))
MAX_ATTEMPTS = int(os.environ.get("AOB_MAX_ATTEMPTS", "3"))
# Delay before attempt 2 and 3; jitter is added on top. Demo values, not production defaults.
RETRY_DELAYS = [float(x) for x in os.environ.get("AOB_RETRY_DELAYS", "5,15").split(",")]
RESOURCE_BACKOFF_SECONDS = float(os.environ.get("AOB_RESOURCE_BACKOFF", "10"))

# Worker health thresholds derived from heartbeat age.
UNHEALTHY_AFTER = float(os.environ.get("AOB_UNHEALTHY_AFTER", "8"))
OFFLINE_AFTER = float(os.environ.get("AOB_OFFLINE_AFTER", "20"))

API_URL = os.environ.get("AOB_API_URL", "http://127.0.0.1:8000")
AOB_ENV = os.environ.get("AOB_ENV", "development").strip().lower() or "development"
AOB_ENABLE_LEGACY_V1 = os.environ.get("AOB_ENABLE_LEGACY_V1", "1" if AOB_ENV != "production" else "0").lower() in (
    "1", "true", "yes"
)

STEPS = [
    "NORMALIZE_INPUT",
    "LOAD_CONTEXT",
    "LLM_INFERENCE",
    "VALIDATE_OUTPUT",
    "SAVE_RESULT",
    "HANDOFF",
]
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "MANUAL_REVIEW")

# ---- Private live demo (Auth0 + Kaggle GPU batch) ------------------------------------------
AUTH0_DOMAIN = os.environ.get("AOB_AUTH0_DOMAIN", os.environ.get("AUTH0_DOMAIN", ""))
AUTH0_AUDIENCE = os.environ.get("AOB_AUTH0_AUDIENCE", os.environ.get("AUTH0_AUDIENCE", ""))
ALLOWED_USERS_FILE = Path(os.environ.get("AOB_ALLOWED_USERS_FILE", ROOT / "allowed_users.json"))
DEMO_DB_PATH = Path(os.environ.get("AOB_DEMO_DB_PATH", RUNTIME_DIR / "demo.db"))
CROSS_CLOUD_ENABLED = os.environ.get("AOB_CROSS_CLOUD_ENABLED", "false").lower() == "true"
REMOTE_RUNS_ENABLED = os.environ.get("AOB_REMOTE_RUNS_ENABLED", "false").lower() == "true"

# Kaggle -> FastAPI events. Each run gets its own signing key derived from this secret.
# The environment variable remains useful for local development; production mounts the
# secret at EVENT_SECRET_FILE instead.
EVENT_SECRET = os.environ.get("AOB_EVENT_SECRET", "")
EVENT_SECRET_FILE = Path(os.environ.get("AOB_EVENT_SECRET_FILE", "/run/secrets/aob_event_secret"))


def get_event_secret() -> str:
    """Return the mounted event secret, falling back to the development env var."""
    try:
        if EVENT_SECRET_FILE.is_file():
            return EVENT_SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        if AOB_ENV == "production":
            return ""
    return EVENT_SECRET.strip()
EVENTS_PUBLIC_URL = os.environ.get("AOB_EVENTS_URL", "")  # e.g. https://demo.example.com/api/events
TRACE_PUBLIC_URL = os.environ.get("AOB_TRACE_URL", "")  # e.g. https://demo.example.com/api/traces
STAGING_EVENTS_URL = "http://event-ingress:8011/api/events"
EVENT_MAX_BYTES = 64 * 1024
EVENT_MAX_SKEW_SECONDS = 300
EVENT_MAX_PER_RUN = 1000
EVENT_KEY_VERSION = int(os.environ.get("AOB_EVENT_KEY_VERSION", "1"))
TRACE_SCHEMA_VERSION = 1
TRACE_MAX_BATCH_BYTES = 512 * 1024
TRACE_MAX_BATCHES_PER_RUN = 32
OTLP_MAX_BYTES = int(os.environ.get("AOB_OTLP_MAX_BYTES", str(2 * 1024 * 1024)))
OTLP_MAX_SPANS = int(os.environ.get("AOB_OTLP_MAX_SPANS", "1000"))
OTLP_SOURCE_TOKENS = os.environ.get("AOB_OTLP_SOURCE_TOKENS", "")
OTLP_READ_SOURCES = os.environ.get("AOB_OTLP_READ_SOURCES")
EVENT_FINALIZATION_GRACE_SECONDS = float(os.environ.get("AOB_EVENT_FINALIZATION_GRACE_SECONDS", "300"))


def get_otlp_source_tokens() -> dict[str, str]:
    """Return source_id -> bearer token mappings, never accepting source identity from the client."""
    path = os.getenv("AOB_OTLP_SOURCE_TOKENS_FILE")
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return {}  # Never fall back to an ambient token when a mount is configured.
    else:
        raw = OTLP_SOURCE_TOKENS.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(value, dict) or not all(k and isinstance(v, str) and v and v.isascii()
                                             for k, v in value.items()):
        return {}
    return value if len(set(value.values())) == len(value) else {}


def get_otlp_read_sources() -> list[str]:
    if OTLP_READ_SOURCES is None:
        return list(get_otlp_source_tokens())  # Existing single-process installations.
    sources = json.loads(OTLP_READ_SOURCES)
    if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
        raise ValueError("AOB_OTLP_READ_SOURCES must be a JSON list of source IDs")
    return list(dict.fromkeys(sources))

KAGGLE_BIN = os.environ.get("AOB_KAGGLE_BIN", "kaggle")
KAGGLE_NOTEBOOK_DIR = Path(os.environ.get("AOB_KAGGLE_NOTEBOOK_DIR", ROOT / "kaggle-notebook"))
KAGGLE_ACCELERATOR = os.environ.get("AOB_KAGGLE_ACCELERATOR", "NvidiaTeslaT4")
KAGGLE_PUSH_TIMEOUT = float(os.environ.get("AOB_KAGGLE_PUSH_TIMEOUT", "120"))

DEMO_MIN_INTERVAL_SECONDS = float(os.environ.get("AOB_DEMO_MIN_INTERVAL", "100"))  # per user
DEMO_MAX_RUNS_PER_DAY = int(os.environ.get("AOB_DEMO_MAX_RUNS_PER_DAY", "35"))  # per user
DEMO_MAX_RUN_SECONDS = float(os.environ.get("AOB_DEMO_MAX_RUN_SECONDS", str(3 * 3600)))
DEMO_SUBMIT_TIMEOUT_SECONDS = KAGGLE_PUSH_TIMEOUT + 60
DEMO_SYNC_INTERVAL_SECONDS = float(os.environ.get("AOB_DEMO_SYNC_INTERVAL", "15"))
# A terminal Kaggle status seen this soon after a push may belong to the previous notebook version.
DEMO_STALE_STATUS_GRACE_SECONDS = float(os.environ.get("AOB_DEMO_STALE_GRACE", "120"))
