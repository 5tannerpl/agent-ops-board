"""Central configuration, overridable through AOB_* environment variables."""
import os
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
AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "")
AUTH0_AUDIENCE = os.environ.get("AUTH0_AUDIENCE", "")
ALLOWED_USERS_FILE = Path(os.environ.get("AOB_ALLOWED_USERS_FILE", ROOT / "allowed_users.json"))
DEMO_DB_PATH = Path(os.environ.get("AOB_DEMO_DB_PATH", RUNTIME_DIR / "demo.db"))

# Kaggle -> FastAPI events. Each run gets its own signing key derived from this secret.
EVENT_SECRET = os.environ.get("AOB_EVENT_SECRET", "")
EVENTS_PUBLIC_URL = os.environ.get("AOB_EVENTS_URL", "")  # e.g. https://demo.example.com/api/events
EVENT_MAX_BYTES = 64 * 1024
EVENT_MAX_SKEW_SECONDS = 300
EVENT_MAX_PER_RUN = 1000

KAGGLE_BIN = os.environ.get("AOB_KAGGLE_BIN", "kaggle")
KAGGLE_NOTEBOOK_DIR = Path(os.environ.get("AOB_KAGGLE_NOTEBOOK_DIR", ROOT / "kaggle-notebook"))
KAGGLE_ACCELERATOR = os.environ.get("AOB_KAGGLE_ACCELERATOR", "NvidiaTeslaT4")
KAGGLE_PUSH_TIMEOUT = float(os.environ.get("AOB_KAGGLE_PUSH_TIMEOUT", "120"))

DEMO_MIN_INTERVAL_SECONDS = float(os.environ.get("AOB_DEMO_MIN_INTERVAL", "300"))  # per user
DEMO_MAX_RUNS_PER_DAY = int(os.environ.get("AOB_DEMO_MAX_RUNS_PER_DAY", "35"))  # per user
DEMO_MAX_RUN_SECONDS = float(os.environ.get("AOB_DEMO_MAX_RUN_SECONDS", str(3 * 3600)))
DEMO_SUBMIT_TIMEOUT_SECONDS = KAGGLE_PUSH_TIMEOUT + 60
DEMO_SYNC_INTERVAL_SECONDS = float(os.environ.get("AOB_DEMO_SYNC_INTERVAL", "15"))
# A terminal Kaggle status seen this soon after a push may belong to the previous notebook version.
DEMO_STALE_STATUS_GRACE_SECONDS = float(os.environ.get("AOB_DEMO_STALE_GRACE", "120"))
